from petkit_local.devices.base import Device
from petkit_local.ha.categories import get_entities_for_device
from petkit_local.ha.discovery import (
    build_discovery_payload, discovery_topic, command_topic_for,
)

DEVICE_TYPES = ["t5", "t6", "t7", "t3", "t4", "d4h", "d4sh", "d3", "d4", "w7h", "w5", "k2", "k3"]


def _build_all(device):
    out = []
    for e in get_entities_for_device(device):
        out.append((e, build_discovery_payload(
            entity=e, device_id=device.petkit_id, device_type=device.device_type,
            device_name="dev", serial_number="SN",
            state_topic=f"petkit-local/{device.petkit_id}/state",
        )))
    return out


def test_unique_ids_unique_per_device():
    for dt in DEVICE_TYPES:
        d = Device(device_type=dt, petkit_id=1, serial_number="SN")
        uids = [p["unique_id"] for _, p in _build_all(d)]
        assert len(uids) == len(set(uids)), f"{dt}: duplicate unique_id"


def test_command_topics_unique_and_per_entity():
    d = Device(device_type="t5", petkit_id=7, serial_number="SN")
    cmd_topics = []
    for e, p in _build_all(d):
        if e.component in ("switch", "button", "number", "select"):
            assert "command_topic" in p
            assert p["command_topic"] == command_topic_for(7, e)
            cmd_topics.append(p["command_topic"])
    assert len(cmd_topics) == len(set(cmd_topics)), "command topics collide"
    # No two settable entities share a topic -> commands are routable.
    assert all(t.startswith("petkit-local/7/cmd/") for t in cmd_topics)


def test_switch_has_state_and_command():
    d = Device(device_type="t5", petkit_id=1, serial_number="SN")
    for e, p in _build_all(d):
        if e.component == "switch":
            assert "state_topic" in p and "command_topic" in p
            assert p["state_on"] == "ON" and p["state_off"] == "OFF"
            assert "value_template" in p


def test_button_has_no_state_topic():
    d = Device(device_type="t5", petkit_id=1, serial_number="SN")
    for e, p in _build_all(d):
        if e.component == "button":
            assert "state_topic" not in p
            assert p["payload_press"] == e.key


def test_no_mqtt_camera_entity_is_published():
    """There used to be one, and it could never show anything: HA's MQTT camera
    renders bytes on a topic and nothing published to that topic. Live view is a
    URL now (`stream_url`), which MQTT discovery cannot express as a camera."""
    d = Device(device_type="t5", petkit_id=1, serial_number="SN")
    assert not [p for e, p in _build_all(d) if e.component == "camera"]


def test_image_uses_image_topic_not_url():
    d = Device(device_type="t5", petkit_id=1, serial_number="SN")
    imgs = [p for e, p in _build_all(d) if e.component == "image"]
    assert imgs, "T5 should expose a Last Snapshot image entity"
    for p in imgs:
        assert "url_topic" not in p
        assert p["image_topic"] == "petkit-local/1/last_snapshot"
        assert p["content_type"] == "image/jpeg"
        assert "state_topic" not in p


def test_discovery_topic_format():
    d = Device(device_type="t5", petkit_id=42, serial_number="SN")
    e = get_entities_for_device(d)[0]
    t = discovery_topic(e, 42, "homeassistant")
    assert t.startswith("homeassistant/")
    assert t.endswith("/config")


def test_value_template_well_formed():
    d = Device(device_type="t5", petkit_id=1, serial_number="SN")
    for _entity, p in _build_all(d):
        vt = p.get("value_template")
        if vt is None:
            continue
        # balanced Jinja braces
        assert vt.count("{{") == vt.count("}}")
        assert "{{" in vt and "}}" in vt


def test_select_state_is_the_label_not_the_raw_device_value():
    """HA validates a select's state against its `options`, so publishing the
    raw device value made every select log `Invalid option ... '2'`."""
    from petkit_local.ha.discovery import _value_template
    d = Device(device_type="t5", petkit_id=1, serial_number="SN")
    sel = next(e for e in get_entities_for_device(d) if e.key == "sand_type")

    tpl = _value_template(sel)
    assert "sandType" in tpl
    for label in sel.options:
        assert f"'{label}'" in tpl
    # device enum values, not option indexes
    for v in sel.option_values:
        assert f"{v}:" in tpl or f"{v}: " in tpl
    # unmapped value publishes nothing rather than an invalid option
    assert "{% if v in m %}" in tpl


def test_select_without_explicit_values_maps_by_index():
    from petkit_local.ha.discovery import _value_template
    d = Device(device_type="w7h", petkit_id=1, serial_number="SN")
    sel = next(e for e in get_entities_for_device(d) if e.key == "flow_mode")
    tpl = _value_template(sel)
    assert "0: 'do_not_flow'" in tpl and "1: 'continuous'" in tpl


def test_select_template_structure_is_balanced_and_complete():
    """The suite never renders these templates (HA does, with its own Jinja
    globals and state), so assert the structure instead. Real rendering
    is verified against Home Assistant itself after deploy: the symptom was
    `Invalid option for select....` in `ha core logs`, which must disappear."""
    from petkit_local.ha.discovery import _value_template
    for dtype, key in (("t5", "sand_type"), ("t5", "cleaning_interval"),
                       ("w7h", "flow_mode")):
        d = Device(device_type=dtype, petkit_id=1, serial_number="SN")
        sel = next(e for e in get_entities_for_device(d) if e.key == key)
        tpl = _value_template(sel)
        assert tpl.count("{%") == tpl.count("%}")
        assert tpl.count("{{") == tpl.count("}}")
        assert tpl.count("{") == tpl.count("}")
        # every option is reachable
        for label in sel.options:
            assert f"'{label}'" in tpl, f"{dtype}/{key}: {label} missing from template"


def test_text_entity_respects_ha_max_length_limit():
    """HA's MQTT text platform rejects the entire discovery message when
    `max` > 255, so the schedule entities never appeared at all."""
    d = Device(device_type="t5", petkit_id=1, serial_number="SN")
    texts = [p for e, p in _build_all(d) if e.component == "text"]
    assert texts, "T5 should expose schedule text entities"
    for p in texts:
        assert p["max"] <= 255


def test_templates_default_missing_keys_to_avoid_ha_warnings():
    """A key the device hasn't reported yet is Jinja Undefined; without a
    default HA logs a template warning on every single publish."""
    d = Device(device_type="t5", petkit_id=1, serial_number="SN")
    for e, p in _build_all(d):
        vt = p.get("value_template")
        if not vt or not e.value_path:
            continue
        assert "default(" in vt, f"{e.component}/{e.key} has an unguarded template: {vt}"


def test_timestamp_sensors_render_none_not_empty_when_unset():
    """HA parses timestamp states with `parse_datetime` and warns on '';
    it maps the literal 'None' (PAYLOAD_NONE) to an unknown state instead."""
    d = Device(device_type="t5", petkit_id=1, serial_number="SN")
    stamped = [(e, p) for e, p in _build_all(d) if e.device_class == "timestamp"]
    assert stamped, "T5 should have timestamp sensors (last clean/visit)"
    for e, p in stamped:
        assert "'None'" in p["value_template"], f"{e.key}: {p['value_template']}"


def _render(template: str, doc: dict):
    """Render a discovery value_template the way HA would."""
    from jinja2 import Template
    return Template(template).render(value_json=doc)


def test_device_status_decodes_the_work_mode_instead_of_publishing_a_number():
    """The sensor used to publish a bare int, so HA showed "0" and the panel
    showed "0". `decode.work_mode_name` existed for exactly this and its
    docstring already claimed the sensor used it -- nothing called it."""
    from petkit_local.devices.state_parsers import WORK_MODE_IDLE

    d = Device(device_type="t5", petkit_id=1, serial_number="SN")
    e, p = next((e, p) for e, p in _build_all(d) if e.key == "device_status")

    assert _render(p["value_template"], {"state": {"workingState": 9}}) == "maintenance"
    assert _render(p["value_template"], {"state": {"workingState": 0}}) == "cleaning"
    assert _render(p["value_template"], {"state": {"workingState": WORK_MODE_IDLE}}) == "idle"

    # An enum value no capture has shown must stay VISIBLE as its raw number.
    # A select blanks an unmapped value because HA rejects one that is not a
    # declared option; a sensor has no such rule, and blanking would hide a
    # state the box is really in. Hence no `device_class="enum"` either, which
    # would reintroduce exactly that validation.
    assert _render(p["value_template"], {"state": {"workingState": 77}}) == "77"
    assert p.get("device_class") != "enum"
    assert "options" not in p, "a sensor's labels are for the template only"

    # Unreported stays empty rather than rendering the word "None".
    assert _render(p["value_template"], {"state": {}}) == ""


def test_litter_weight_is_published_in_the_unit_the_device_reports():
    """The device sends `litter.weight` as whole grams (5469 in real reports).
    This declared kg, so HA was told the box held 5469 kg."""
    d = Device(device_type="t5", petkit_id=1, serial_number="SN")
    e, p = next((e, p) for e, p in _build_all(d) if e.key == "litter_weight")
    assert p["unit_of_measurement"] == "g"

    # Pet Weight measures the same quantity off the same scale, so a mismatch
    # between the two is the tell that one of them is wrong.
    _, pet = next((e, p) for e, p in _build_all(d) if e.key == "pet_weight")
    assert pet["unit_of_measurement"] == p["unit_of_measurement"]


def test_event_entity_payload_reaches_ha_unwrapped():
    """HA's MQTT event platform renders value_template FIRST and then requires
    the result to still be a JSON object carrying `event_type` (extra keys
    become the fired event's attributes). The old template here extracted the
    bare string, so HA rejected every event — "No valid JSON event payload
    detected, value after processing payload 'clean_over'" — and the entities
    never left `unknown`. The publisher already sends exactly the JSON HA
    wants, so the config must not template it at all."""
    d = Device(device_type="t6", petkit_id=7, serial_number="SN")
    events = [(e, p) for e, p in _build_all(d) if e.component == "event"]
    assert events, "a T6 announces event entities"
    for e, p in events:
        assert "value_template" not in p
        assert p["state_topic"] == f"petkit-local/7/event/{e.unique_id_suffix}"
        assert p["event_types"] == e.options
