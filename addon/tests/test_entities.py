from petkit_local.devices.base import Device
from petkit_local.ha.categories import CATEGORY_SPECS, spec_for_device
from petkit_local.ha.categories import get_entities_for_device, get_mqtt_state_topics
from petkit_local.utils.const import DEVICE_TYPES_ALL

VALID_COMPONENTS = {
    "sensor", "binary_sensor", "switch", "button", "number", "select", "camera",
    "event", "text", "image", "time",
}

DEVICE_TYPES = ["t5", "t6", "t7", "t3", "t4", "d4h", "d4sh", "d3", "d4", "w7h", "w5", "k2", "k3"]

# Golden entity counts per device type. An entity's identity in Home Assistant
# is its unique_id, so silently dropping, adding or renaming one orphans it in
# every live installation and loses its recorded history. Changing a number
# here is allowed, but only ever deliberately.
# Changed 2026-07-29 by the unbacked-entity sweep (see tests/test_entity_backing.py).
# Every removal is backed by real firmware: the field's name appears nowhere in
# the extracted binaries, while every field kept beside it does. They came from
# the reference integration, which models PetKit's CLOUD API — whose field names
# are not the device's.
#   * litter -2: garbage_bag_state, purification_days (absent from T5 firmware);
#   * feeder  -2: feed_tone, disturb_mode (absent from D4SH firmware);
#   * the deodorizer and sound entities moved from the base litter/feeder lists
#     onto the camera-gated ones, which is where their defaults were already
#     seeded — so non-camera models no longer publish hardware they lack.
EXPECTED_ENTITY_COUNTS = {
    # Litter models dropped 5 on 2026-07-30: `sand_lack`, `weight_error`,
    # `frequent_use`, `low_power` and `litter_tray`, none of which any device
    # has ever filled (see the REMOVED note in ha/entities/sensors.py).
    #
    # Camera litters gained 6 on 2026-07-31: the `sensor{}` hall switches, read
    # live off a running T5. The W7H went 31 -> 61 the same day: its own field
    # map replaced nine entities it can never fill (filter, battery, `power`
    # buttons) with the 39 its `property/post` actually carries.
    #
    # 61 -> 67 on 2026-08-02: the three water-treatment buttons its `ctrl`
    # accepts as `start_action`, plus the three `event` entities its kinds fire
    # — which until then published to discovery topics it had never announced.
    #
    # The embedded-Linux feeders moved on 2026-08-08, from the two real D4SH
    # reports in issue #2 and that firmware's own state builder. Both lose four
    # controls nothing on this hardware fills (`food_low`, `food_in_bowl`,
    # `food_bowl_pct`, `battery_installed` — the device says `food1`/`food2`,
    # and `batV` rather than `batteryPower`) and gain the fields it does send.
    # 42 -> 50 on the D4H, which reports one hopper; the D4SH keeps its second
    # hopper sensor and adds the two per-hopper feed buttons and their portion
    # numbers, for 54.
    #
    # 2026-08-09, from two capture-derived settings maps (T6 and W7H) plus a
    # proxied T5. Every litter box gains `click_ok`; a camera one gains five
    # settings this add-on had been seeding and serving to the device with no
    # control for them (pet/wander/toilet detection, voice prompt, voice DND)
    # and three buttons (Light, Power Off, Power On) — 45 -> 46 and 73 -> 82.
    # The T6 is 83: it drops `reset_n50`, which it has no cartridge for and
    # whose code its own app uses for Pack, and adds Pack and Open Sealed Door.
    #
    # 67 -> 84 on the W7H: nine camera and voice switches (it had a camera
    # entity and no way to switch the camera off), the two drain cycles and
    # their two times, volume, voice language, and the same two power buttons.
    # t6 +6 on 2026-08-12: the bagging mechanism it reports in every
    # property post and nothing here read. No other litter box has it.
    "t3": 46, "t4": 46, "t5": 82, "t6": 89, "t7": 82,
    "feeder": 25, "feedermini": 25, "d3": 25, "d4": 25, "d4s": 25,
    # 2026-08-12: replaced `enable_feed_video` button with individual switches
    # (feed_picture, eat_video, voice_prompt, voice_disturb_mode, disturb_mode)
    # and added selected_sound number + play_sound button. Net +6.
    "d4h": 57, "d4sh": 61,
    "w4": 24, "w5": 24, "ctw2": 24, "ctw3": 24, "w7h": 84,
    "k2": 12, "k3": 12,
}

# (non-camera model, camera model) of the same category. The camera bundle must
# be APPENDED to the shared base, never interleaved, or the two models disagree
# about entity order for no reason.
CAMERA_PAIRS = [("t4", "t5"), ("d4", "d4h"), ("w5", "w7h")]


def test_all_components_are_valid_ha_platforms():
    for dt in DEVICE_TYPES:
        d = Device(device_type=dt, petkit_id=1, serial_number="SN")
        for e in get_entities_for_device(d):
            assert e.component in VALID_COMPONENTS, f"{dt}: bad component {e.component!r}"


def test_camera_devices_get_the_media_bundle():
    for dt in ("t5", "t6", "t7", "d4h", "d4sh", "w7h"):
        d = Device(device_type=dt, petkit_id=1, serial_number="SN")
        keys = [e.key for e in get_entities_for_device(d)]
        # The camera bundle is the two media entities plus the live-view URL.
        # There is deliberately no `camera` component: see ha/entities/camera.py.
        assert "last_snapshot" in keys, f"{dt} should have the snapshot entity"
        assert "stream_url" in keys, f"{dt} should have the live-view URL"


def test_non_camera_devices_do_not_get_the_media_bundle():
    """Checked by key, not by component: since the MQTT camera entity was
    removed no model has a `camera` component at all, so asserting on that would
    pass for every device and prove nothing."""
    for dt in ("t3", "t4", "d3", "d4", "w5", "k3"):
        d = Device(device_type=dt, petkit_id=1, serial_number="SN")
        keys = [e.key for e in get_entities_for_device(d)]
        for absent in ("last_snapshot", "last_clip", "stream_url"):
            assert absent not in keys, f"{dt} should NOT have {absent}"


def test_every_device_type_produces_entities():
    for dt in DEVICE_TYPES:
        d = Device(device_type=dt, petkit_id=1, serial_number="SN")
        assert len(get_entities_for_device(d)) > 0, f"{dt} produced no entities"


def test_state_topics_present_for_known_categories():
    for dt in ("t5", "t4", "d4h", "d4", "w5", "k3"):
        d = Device(device_type=dt, petkit_id=1, serial_number="SN")
        assert get_mqtt_state_topics(d), f"{dt} has no MQTT state topics"


def test_entity_keys_unique_within_device():
    for dt in DEVICE_TYPES:
        d = Device(device_type=dt, petkit_id=1, serial_number="SN")
        keys = [e.unique_id_suffix for e in get_entities_for_device(d)]
        dupes = {k for k in keys if keys.count(k) > 1}
        assert not dupes, f"{dt}: duplicate entity keys {dupes}"


# --- category table ---

def test_entity_count_per_device_type_is_unchanged():
    for dt, expected in EXPECTED_ENTITY_COUNTS.items():
        d = Device(device_type=dt, petkit_id=1, serial_number="SN")
        got = len(get_entities_for_device(d))
        assert got == expected, f"{dt}: {got} entities, expected {expected}"


def test_every_supported_device_type_belongs_to_a_category():
    covered = set()
    for spec in CATEGORY_SPECS.values():
        covered |= spec.device_types
    assert covered == DEVICE_TYPES_ALL


def test_categories_do_not_overlap():
    # spec_for_device returns the FIRST matching category, so an overlap would
    # silently give one of them entities that belong to the other.
    seen = set()
    for name, spec in CATEGORY_SPECS.items():
        clash = seen & spec.device_types
        assert not clash, f"{name} shares device types with an earlier category: {clash}"
        seen |= spec.device_types


def test_unknown_device_type_is_classified_as_nothing():
    d = Device(device_type="zz9", petkit_id=1, serial_number="SN")
    assert spec_for_device(d) is None
    assert get_entities_for_device(d) == []
    assert get_mqtt_state_topics(d) == []


def test_camera_bundle_is_appended_to_the_shared_base():
    """The camera model adds entities; it never reshuffles the shared ones.

    Stated as "the entities both models publish appear in the same relative
    order" rather than "the camera list starts with the plain list". The
    stricter form stopped being true once a model could be gated: a W7H drops
    nine fountain entities its hardware has no field for (`filter_percent`,
    `battery`, ...) and adds its own, so it is not a prefix-superset of a W5 —
    by design. What must still hold is that nothing gets reordered, because
    that is the part with no reason behind it.
    """
    for plain_type, camera_type in CAMERA_PAIRS:
        plain = get_entities_for_device(Device(device_type=plain_type, petkit_id=1))
        camera = get_entities_for_device(Device(device_type=camera_type, petkit_id=1))
        assert len(camera) > len(plain), f"{camera_type} should add camera entities"

        plain_keys = [e.key for e in plain]
        camera_keys = [e.key for e in camera]
        shared = set(plain_keys) & set(camera_keys)
        assert shared, f"{camera_type} and {plain_type} share no entities at all"
        assert [k for k in camera_keys if k in shared] == \
               [k for k in plain_keys if k in shared], (
            f"{camera_type} reorders the entities it shares with {plain_type}")


def test_camera_state_topics_are_appended_to_the_shared_base():
    for plain_type, camera_type in CAMERA_PAIRS:
        plain = get_mqtt_state_topics(Device(device_type=plain_type, petkit_id=1))
        camera = get_mqtt_state_topics(Device(device_type=camera_type, petkit_id=1))
        assert camera[:len(plain)] == plain, f"{camera_type} reorders {plain_type} topics"


def test_purifiers_have_no_camera_bundle():
    # K2/K3 are BLE-only accessories. They take the same has_camera path as
    # every other category purely so the table stays uniform.
    spec = CATEGORY_SPECS["purifier"]
    assert spec.camera_entities == ()
    assert spec.camera_state_topics == ()
    assert spec.entities_for(has_camera=True) == spec.entities_for(has_camera=False)


def test_entity_list_is_not_shared_between_calls():
    # Callers treat the result as their own; a shared list would let one
    # device's mutation leak into every other device of that category.
    d = Device(device_type="t5", petkit_id=1)
    first = get_entities_for_device(d)
    first.clear()
    assert get_entities_for_device(d), "entity list must be rebuilt per call"


def test_state_topic_list_is_not_shared_between_calls():
    d = Device(device_type="t5", petkit_id=1)
    first = get_mqtt_state_topics(d)
    first.clear()
    assert get_mqtt_state_topics(d), "topic list must be rebuilt per call"


def test_event_entity_options_accept_every_event_the_device_can_fire():
    """HA silently drops a fired event_type the discovery config did not
    declare. The firing side is `normalize.entity_for_event` (codes table ->
    kind -> entity key); the accepting side is the entity's `options`. This
    pins the two together: every MQTT event name that fires an entity a
    device announces must be in that entity's list. A real T6's `light_over`
    was dropped exactly this way — it fires `cleaning_event`, whose
    hand-written list stopped at the cycles a T3 has."""
    from petkit_local.events import codes
    from petkit_local.events.normalize import KIND_TO_ENTITY

    for dt in DEVICE_TYPES:
        d = Device(device_type=dt, petkit_id=1, serial_number="SN")
        events = {e.key: e for e in get_entities_for_device(d)
                  if e.component == "event"}
        for name, code in codes.MQTT_EVENT_TOPICS.items():
            if dt not in code.families:
                continue
            key = KIND_TO_ENTITY.get(code.kind)
            if key is None or key not in events:
                # A kind no entity listens to (pet/motion/system), or an
                # entity this category does not announce: nothing fires, so
                # there is nothing to accept.
                continue
            assert name in events[key].options, (
                f"{dt}: '{name}' fires '{key}' but its event_types omit it")
