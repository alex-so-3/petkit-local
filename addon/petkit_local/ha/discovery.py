"""HA MQTT-discovery model: `EntityDef` plus the payload/topic builders.

Every entity this add-on exposes is declared once as an `EntityDef` (see
`ha/entities/`), and this module is the single place that turns such a
declaration into the JSON Home Assistant's MQTT integration expects. Keeping
the translation here — rather than spelling out discovery dicts per entity —
is what makes ~474 entities across 13 device types tractable, and it is where
the hard-won rules about what HA actually accepts are recorded: several
comments below document discovery payloads that HA *silently* rejects, which
is indistinguishable from "the entity never appeared".

The unit tests never render these templates — only Home Assistant does, with
its own Jinja globals and entity state — so any change to the templates built
here has to be checked against `ha core logs` on a live install.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class EntityDef:
    """Declarative definition of one Home Assistant entity.

    An `EntityDef` is transport-agnostic: it says what the entity IS, while
    this module decides which topics it uses and `ha/commands.py` decides what
    a write to it does. The lists in `ha/entities/` are plain module-level
    constants, so instances are effectively shared singletons — treat them as
    immutable, never mutate one per device.

    Invariant: `key` must be unique across ALL components of a single device,
    not just within its own component. `unique_id` is derived from the key
    alone, and HA requires it to be globally unique.

    Fields:
        component: HA MQTT platform — sensor, binary_sensor, switch, button,
            number, select, text, event or image. It selects the
            per-component branch in `build_discovery_payload` and appears in
            the discovery topic, so an unknown value yields a bare sensor-like
            payload HA will ignore.
        key: stable slug identifying the entity within its device. Also the
            `payload_press` a button publishes, which is how `ha/commands.py`
            looks the action up in `ALL_ACTIONS`.
        name: human-readable name shown in HA.
        value_path: dot path into the state document published by
            `HAPublisher._build_state` (e.g. `state.sandPercent`,
            `settings.autoWork`, `capabilities.fullVideo`). Empty means the
            whole payload is the value. For settable entities the LAST segment
            doubles as the device settings field written back — see
            `setting_field`.
        device_class: HA device class. `timestamp` additionally changes how
            the value template renders an unset value (see `_value_template`).
        unit: `unit_of_measurement`.
        icon: mdi icon name, used when no device class implies one.
        entity_category: "config" or "diagnostic" to demote the entity out of
            the device's primary controls; empty for a primary entity.
        options: select option LABELS, or — for an `event` entity — the list of
            event_types HA should accept, or — for a `sensor` — the labels a
            raw device enum decodes to (see `_value_template`). Only select and
            event put them in the discovery payload; a sensor's are used by the
            value template alone, so HA never validates against them.
        option_values: device-side values corresponding 1:1 to `options`, for
            selects whose device enum is not a plain 0-based index (e.g. the
            minute intervals 0/5/10/15...). Empty means "use the option index".
        min_value, max_value, step: `number` bounds. Either bound may be None,
            meaning the entity declares none on that side: `ha/commands.py`
            enforces a bound as a hard refusal, so a made-up default would
            reject legal values, and HA reads an absent `min`/`max` as
            unbounded.
        payload_on, payload_off: `switch` payloads, used for BOTH the command
            payload and the state HA matches against.
    """

    component: str
    key: str
    name: str
    value_path: str = ""
    device_class: str = ""
    #: HA statistics class. A running daily counter needs
    #: `total_increasing`, or the long-term statistics treat every midnight
    #: reset as the value genuinely falling and the graph reads as nonsense.
    state_class: str = ""
    unit: str = ""
    icon: str = ""
    entity_category: str = ""
    options: list[str] = field(default_factory=list)
    option_values: list = field(default_factory=list)
    min_value: float | None = None
    max_value: float | None = None
    step: float = 1
    payload_on: str = "ON"
    payload_off: str = "OFF"

    @property
    def unique_id_suffix(self) -> str:
        """The key in the normalised form used in unique_ids and topics."""
        return self.key.replace(" ", "_").lower()

    @property
    def is_settable(self) -> bool:
        """Entity that accepts commands from HA."""
        return self.component in ("switch", "button", "number", "select", "text", "time")

    @property
    def setting_field(self) -> str:
        """Device settings field this entity writes to (last path segment).

        Only meaningful for switch/number/select whose value_path points at
        ``settings.<field>``. Buttons have no field (they trigger actions).
        """
        if not self.value_path:
            return ""
        return self.value_path.split(".")[-1]


def command_topic_for(device_id: int, entity: EntityDef) -> str:
    """Per-entity command topic.

    Every settable entity gets its OWN topic so the receiving side can route
    the command by entity — a single shared topic carrying bare ``ON``/value
    payloads is unroutable (the handler can't tell which setting changed).
    """
    return f"petkit-local/{device_id}/cmd/{entity.unique_id_suffix}"


def build_discovery_payload(
    entity: EntityDef,
    device_id: int,
    device_type: str,
    device_name: str,
    serial_number: str,
    state_topic: str,
    command_topic: str | None = None,
    identifiers: list[str] | None = None,
    availability_topic: str | None = None,
) -> dict[str, Any]:
    """Build the JSON config HA's MQTT discovery expects for one entity.

    Args:
        identifiers: Overrides the `petkit_{device_id}` device identity, which
            also re-roots every derived unique_id. Together with
            `availability_topic` this is what lets a caller publish a *virtual*
            device outside the `petkit_{device_id}` /
            `petkit-local/{device_id}/...` namespace — used for per-pet devices
            (`ha/publisher.py::publish_pet_discovery`), whose id space is
            unrelated to real device ids and would otherwise collide with one
            (e.g. pet id 1 vs. device id 1).
        command_topic: Overrides the per-entity default from
            `command_topic_for`; ignored for read-only components.

    Returns:
        A dict always carrying `name`, `unique_id`, `object_id`, `device`
        (identifiers/name/manufacturer/model/serial_number) and `availability`
        (topic + online/offline payloads). Read-only components additionally
        carry `state_topic` + `value_template` — except `event`, whose payload
        must reach HA's event platform as raw JSON (see the branch below);
        settable ones carry `command_topic` plus their component-specific keys
        (switch payloads, number step and whichever of min/max the entity
        declares, select options, text max, event event_types); the
        the image component replaces `state_topic` with its own
        `topic`/`image_topic`. Optional `device_class`, `unit_of_measurement`,
        `icon` and `entity_category` appear only when set on the entity —
        publishing them empty makes HA reject the whole message.
    """
    ids = identifiers or [f"petkit_{device_id}"]
    uid = f"{ids[0]}_{entity.unique_id_suffix}"
    payload = {
        "name": entity.name,
        "unique_id": uid,
        "object_id": uid,
        "state_topic": state_topic,
        "value_template": _value_template(entity),
        "device": {
            "identifiers": ids,
            "name": device_name,
            "manufacturer": "PetKit",
            "model": device_type.upper(),
            "serial_number": serial_number,
        },
        "availability": {
            "topic": availability_topic or f"petkit-local/{device_id}/availability",
            "payload_available": "online",
            "payload_not_available": "offline",
        },
    }

    if entity.device_class:
        payload["device_class"] = entity.device_class
    if entity.state_class:
        payload["state_class"] = entity.state_class
    if entity.unit:
        payload["unit_of_measurement"] = entity.unit
    if entity.icon:
        payload["icon"] = entity.icon
    if entity.entity_category:
        payload["entity_category"] = entity.entity_category

    cmd = command_topic or command_topic_for(device_id, entity)

    if entity.component == "switch":
        payload["command_topic"] = cmd
        payload["payload_on"] = entity.payload_on
        payload["payload_off"] = entity.payload_off
        payload["state_on"] = entity.payload_on
        payload["state_off"] = entity.payload_off

    elif entity.component == "button":
        payload["command_topic"] = cmd
        payload["payload_press"] = entity.key
        payload.pop("state_topic", None)
        payload.pop("value_template", None)

    elif entity.component == "number":
        payload["command_topic"] = cmd
        if entity.min_value is not None:
            payload["min"] = entity.min_value
        if entity.max_value is not None:
            payload["max"] = entity.max_value
        payload["step"] = entity.step

    elif entity.component == "select":
        payload["command_topic"] = cmd
        payload["options"] = entity.options

    elif entity.component == "time":
        # HA's MQTT time platform speaks `HH:MM:SS` in both directions; the
        # device stores seconds since midnight. `_value_template` converts on
        # the way out, `ha/commands.py::_coerce_time` on the way in. No `format`
        # key is published: HA's time platform has no such option, and an
        # unknown key makes it reject the whole discovery message.
        payload["command_topic"] = cmd

    elif entity.component == "text":
        # Free-form control (used for raw schedule JSON). Reads from the state
        # doc via value_template, writes the raw string back via command_topic.
        # HA's MQTT text platform hard-caps `max` at 255 and REJECTS the whole
        # discovery message otherwise ("max text length must be <= 255"), so a
        # larger value silently meant no entity at all. A schedule longer than
        # this can't be edited from HA — use the web panel for those.
        payload["command_topic"] = cmd
        payload["max"] = 255

    elif entity.component == "event":
        # Momentary HA event entity. Reads a dedicated non-retained topic where
        # `HAPublisher.publish_event` fires {"event_type": ..., <attributes>}
        # when the device reports an event. NO value_template: HA's event
        # platform renders the template first and then requires the RESULT to
        # still be a JSON object carrying `event_type` (extra keys become the
        # event's attributes). Extracting the string here made HA reject every
        # single event — "No valid JSON event payload detected, value after
        # processing payload 'clean_over'" — so the entities never left
        # `unknown` and could not trigger an automation.
        payload.pop("value_template", None)
        payload["state_topic"] = f"petkit-local/{device_id}/event/{entity.unique_id_suffix}"
        payload["event_types"] = entity.options

    elif entity.component == "image":
        # Raw (non-base64) image bytes retained on `image_topic` — no
        # reachable-URL scheme is needed, unlike `url_topic`: media/pipeline.py
        # (via HAPublisher.publish_media_ready) pushes the actual JPEG bytes
        # here directly: the bytes are the entity's whole content.
        payload.pop("value_template", None)
        payload.pop("state_topic", None)
        payload["image_topic"] = f"petkit-local/{device_id}/{entity.unique_id_suffix}"
        payload["content_type"] = "image/jpeg"

    return payload


def discovery_topic(entity: EntityDef, device_id: int, prefix: str = "homeassistant",
                     identifiers: list[str] | None = None) -> str:
    """Retained config topic HA watches: `{prefix}/{component}/{uid}/config`.

    `identifiers` must match what is passed to `build_discovery_payload`, or
    the published config lands under a unique_id that the payload doesn't
    claim and HA ends up with an orphan entity.
    """
    uid_base = identifiers[0] if identifiers else f"petkit_{device_id}"
    uid = f"{uid_base}_{entity.unique_id_suffix}"
    return f"{prefix}/{entity.component}/{uid}/config"


def _value_template(entity: EntityDef) -> str:
    """Jinja that extracts this entity's value from the state document."""
    if not entity.value_path:
        return "{{ value_json }}"

    parts = entity.value_path.split(".")
    accessor = "value_json"
    for p in parts:
        accessor += f".{p}" if p.isidentifier() else f"['{p}']"

    # `| default(...)` matters: a key the device hasn't reported yet is Jinja
    # Undefined, and HA logs a "Template variable warning: 'dict object' has
    # no attribute ..." for every publish. Defaulting keeps the rendered value
    # identical while silencing the noise.
    if entity.component in ("binary_sensor", "switch"):
        return "{{ 'ON' if " + accessor + " | default(false) else 'OFF' }}"
    if entity.component == "select":
        return _select_value_template(entity, accessor)
    if entity.component == "time":
        return _time_value_template(accessor)
    if entity.component == "sensor" and entity.options:
        return _enum_sensor_value_template(entity, accessor)
    if entity.device_class == "timestamp":
        # A timestamp sensor is the one case where an empty payload is an
        # error: HA runs it through `parse_datetime` and logs "Invalid state
        # message ''" on every publish until the device reports one. HA maps
        # the literal string "None" (PAYLOAD_NONE) to an unknown state, which
        # is what we actually mean before the first event.
        return "{{ " + accessor + " | default('None') or 'None' }}"
    return "{{ " + accessor + " | default('') }}"


def _time_value_template(accessor: str) -> str:
    """Render seconds-since-midnight as the `HH:MM:SS` HA's time platform wants.

    `| int(-1)` rather than a `number` test, because the value arrives from a
    device-supplied settings dict: it may be a string, and it may be absent
    entirely (none of these fields is seeded — the fountain reports no settings
    at all until it is asked). Both land on -1 and render nothing.

    Rendering NOTHING for an unknown value is the same choice
    `_select_value_template` makes, for the same reason: there is no honest
    time of day to publish before the device names one, and inventing 00:00:00
    would show a schedule nobody set. Whether HA logs about the empty payload
    the way it does for a timestamp sensor is unverified — no W7H has been
    available to watch, and only Home Assistant renders these templates.
    """
    return ("{% set v = " + accessor + " | default(-1) | int(-1) %}"
            "{% if v >= 0 %}"
            "{{ '%02d:%02d:%02d' | format(v // 3600, v // 60 % 60, v % 60) }}"
            "{% endif %}")


def _select_value_template(entity: EntityDef, accessor: str) -> str:
    """HA validates a select's state against its declared `options`, so the
    state has to be the LABEL — publishing the raw device value made every
    select log `Invalid option for select.…: '2'` and never track.

    The device value maps to a label either through `option_values` (an
    explicit enum, e.g. sandType 1/2/3) or by position. Unmapped values render
    nothing rather than an invalid option, so HA leaves the entity alone
    instead of erroring.
    """
    values = entity.option_values or list(range(len(entity.options)))
    # strict=False deliberately: a mismatch here must not raise while publishing
    # to a live device. The invariant that the two lists agree is enforced
    # statically instead — see tests/test_entities.py.
    mapping = ", ".join(
        f"{_jinja_literal(v)}: {_jinja_literal(label)}"
        for v, label in zip(values, entity.options, strict=False)
    )
    return ("{% set v = " + accessor + " | default(none) %}"
            "{% set m = {" + mapping + "} %}"
            "{% if v in m %}{{ m[v] }}{% endif %}")


def _enum_sensor_value_template(entity: EntityDef, accessor: str) -> str:
    """Decode a raw device enum to a label, falling back to the raw value.

    A select MUST render a declared option or HA logs `Invalid option` and drops
    the state, so `_select_value_template` renders nothing for an unmapped value.
    A sensor is free-form, and here that difference matters: `codes.WORK_MODES`
    has ten entries but only three (`WORK_MODES_OBSERVED`) have ever been seen
    in a capture, so the rest are inferred. Blanking an unmapped mode would hide
    a real state the device is genuinely in; showing the number keeps it visible
    and debuggable. For the same reason these entities must NOT declare
    `device_class="enum"`, which would make HA reject anything off the list.
    """
    values = entity.option_values or list(range(len(entity.options)))
    mapping = ", ".join(
        f"{_jinja_literal(v)}: {_jinja_literal(label)}"
        for v, label in zip(values, entity.options, strict=False)
    )
    return ("{% set v = " + accessor + " | default(none) %}"
            "{% set m = {" + mapping + "} %}"
            "{% if v in m %}{{ m[v] }}{% elif v is not none %}{{ v }}{% endif %}")


def _jinja_literal(value: object) -> str:
    """Render a Python scalar as a Jinja literal for the select value map.

    Type matters here, not just text: the device reports `sandType` as the
    number 2, so the map key has to be the literal `2` and not `'2'` or the
    `v in m` lookup misses and the select renders nothing.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "\\'") + "'"
