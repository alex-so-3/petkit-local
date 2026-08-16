"""Every published entity must have something that can actually fill it.

An `EntityDef` whose `value_path` nothing can ever produce is published to Home
Assistant and to the panel, and then reads unknown forever — indistinguishable
from a device that simply has not reported yet. Four feeder controls shipped in
that state, and the only reason anyone noticed was someone looking at a blank
card.

This test is the durable form of that audit: it walks every codename and fails
when an entity is added without a backing field, instead of waiting for a user
to spot an empty control.
"""
import ast
import inspect

import pytest

from petkit_local.devices import defaults, state_parsers
from petkit_local.devices.base import Device
from petkit_local.ha.categories import get_entities_for_device
from petkit_local.utils.const import DEVICE_TYPES_ALL

#: `state.*` keys that no state parser produces because they are not device
#: telemetry — each is derived at runtime, and named here with what writes it.
#: Anything NOT in this list must be produced by the model's own parser.
RUNTIME_DERIVED = {
    # events/normalize.py::apply_derived_state, from a completed event
    "lastClean", "lastVisit", "lastFeed", "petWeight",
    # ha/publisher.py::publish_media_ready, when the media pipeline files a clip
    "lastClipPath",
    # ha/publisher.py::_build_state, from the device IP plus the camera patcher
    "streamUrl",
}

#: `settings.*` fields we know are real but deliberately never seed.
#:
#: `payloads.to_device_info` serves `config["settings"]` straight back to the
#: device, so a seeded default is not a display convenience — it is a value we
#: PUSH. Inventing one would silently change the owner's setting, which is why
#: the litter defaults carry a note saying they were checked against a captured
#: `dev_device_info`. For a field we can prove exists but whose value we have
#: never seen, the honest state is "unknown until the device tells us", and the
#: entity populates on the first settings sync.
#:
#: Each entry must name its evidence. Adding one without evidence defeats the
#: whole test.
UNSEEDED_BY_DESIGN = {
    # `feedSound` is seeded for camera feeders but not for non-camera ones
    # (d3, feeder, feedermini, d4, d4s) where the hardware is unconfirmed.
    "feedSound",
    # W7H. Each is a wire name the device's own `ctrl` registers a set handler
    # for, per the reverse-engineered settings map supplied 2026-07-31
    # (`wire_to_set_handler`), so the write lands. What the map does NOT give is
    # the current value or the default, and this device's `property/post`
    # carries no settings at all — so there is nothing to seed from and any
    # number here would be a value we PUSH to somebody's fountain.
    "drinkDetection",
    "vomitDetection",
    "autoFlush",
    "autoWaterChange",
    "cleanWaterLackLight",
    "cleanWaterEmptyLight",
    "wasteWaterFullLight",
    "wifiLightAssist",
    "awDisturbMode",
    "wlDisturbMode",
    # The rest of the W7H's app-visible settings, from the capture-derived map
    # supplied 2026-08-09. Same argument as the block above: real set handlers,
    # no observed current value, and `to_device_info` would push whatever we
    # invented. `upload` and `microLight` in particular are NOT the same fields
    # the litter boxes seed under similar names.
    "camera",
    "upload",
    "microphone",
    "microLight",
    "night",
    "timeDisplay",
    "smartFrame",
    "waterChangeCycle",
    "waterChangeTime",
    "flushCycle",
    "flushTime",
    # These four ARE seeded — for a litter box or a feeder, not for a fountain,
    # and this list is global. They are here for the W7H alone, and the litter
    # and feeder entities that read them stay backed by the real seed rather
    # than by this exemption.
    "systemSoundEnable",
    "toneMode",
    "volume",
    "language",
    # Litter box. The app writes it, and the capture only ever saw it written
    # together with `petDetection` — so what it does on its own is unobserved,
    # and a seeded 0 or 1 would be us deciding.
    "wanderDetection",
    # Seeded until 2026-08-09 as `0`, which is not one of its three values.
    # `to_device_info` served that back as the litter the box is filled with.
    "sandType",
}

#: `state.*` keys whose ONLY backing is a parser passthrough list, with the
#: evidence that a device really sends each one.
#:
#: A passthrough entry means "copy this key if it shows up" — it is a wish, not
#: a source. Five entities (`sandLack`, `petError`, `frequentRestroom`,
#: `lowPower`, `sandTrayState`) lived on that technicality for a year: they were
#: in the tuples, so the string-literal scan found them and this test passed,
#: while no device ever sent one and every sensor read unknown forever. That is
#: the exact failure this file was written to catch, and it walked straight past
#: it. So a passthrough-only key now has to be listed here with its evidence.
PASSTHROUGH_ATTESTED = {
    # The Purobot Ultra's bagging mechanism, from a 67-hour capture of a real
    # T6 (2026-08-11). Counts are out of its 3475 `property/post` frames. The
    # VALUES are not decoded and the entities publish them raw -- what is
    # attested here is only that the field arrives.
    "packageState": "T6 capture 2026-08-11: 3475/3475 property posts",
    "packState": "T6 capture 2026-08-11: 3475 posts, values -1 (3470) / 1 (5)",
    "baggingState": "T6 capture 2026-08-11: 3475 posts, values -1 (3315) / 1 (160)",
    "sealDoorState": "T6 capture 2026-08-11: 3475 posts, values 0 / 1 (179)",
    "boxStoreState": "T6 capture 2026-08-11: 3475 posts, values 0 (3470) / 2 (5)",
    "packageCount": "T6 capture 2026-08-11: 3475 posts, values 10 and 9",
    # In real T5 `ctrl`, and in all 1254 captured litter snapshots.
    "sprayState": "T5 ctrl string table; 1254/1254 captured snapshots",
    "boxState": "T5 ctrl string table; 1254/1254 captured snapshots",
    # In real T5 `ctrl`, beside `lightState` in the same json-builder block.
    # Presence-signalled, so read via PRESENCE_FLAGS rather than directly.
    "refreshState": "T5 ctrl string table; 32/1254 captured snapshots",
    # Fountain and purifier fields. We own neither, so there is no capture of
    # our own; the evidence is that the reference integration reads each one as
    # a real attribute of its parsed device model (snake_case there, camelCase
    # on the wire). Each line names the call site that was checked.
    "lowBattery": "reference integration binary_sensor.py:237 device.low_battery",
    "filterWarning": "reference integration binary_sensor.py:243 device.filter_warning",
    "filterPercent": "reference integration sensor.py:585 device.filter_percent",
    "humidity": "reference integration sensor.py:741 device.state.humidity",
    "refresh": "reference integration sensor.py:757 device.state.refresh",
    "refreshing": "reference integration binary_sensor.py:372 device.refreshing",
    "liquidLack": "reference integration binary_sensor.py:379 device.liquid_lack",
    # W7H (EverSweet Ultra AI). Unlike the fountain rows above, these are not
    # read off a reference model: each is a key of one real `property/post`
    # captured from a W7H on 2026-07-31, and each is named with the same
    # meaning in the reverse-engineered `ctrl` field map supplied alongside it.
    # Two independent sources agreeing on the same 42-key payload is the
    # strongest evidence any row in this file has.
    # These three had their MEANING corrected in 1.4.0. The owner was right and
    # the field map was read the wrong way round: `stg*` is the tray, `wt*` the
    # waste tank. Evidence is the write itself in W7H 456 `ctrl` — the reader
    # feeding `stgFullState` names itself `pk_hmi_get_water_tary_full_sta`, and
    # the one feeding `wtInstall` is the predicate behind "Not work dirty tank
    # unstall". Shape unchanged; only the label was wrong.
    "stgFullState": "W7H ctrl 456: set_prop(0x0d) <- pk_hmi_get_water_tary_full_sta",
    "cwtState": "W7H property/post 2026-07-31; ctrl map params_install_and_levels",
    "wtState": "W7H ctrl 456: set_prop(0x04), logged 'waste tank num now'",
    "heatInstall": "W7H property/post 2026-07-31; ctrl map params_install_and_levels",
    "pumpState": "W7H property/post 2026-07-31; ctrl map params_work_states",
    "waterPumpState": "W7H property/post 2026-07-31; ctrl map params_work_states",
    "addWaterState": "W7H property/post 2026-07-31; ctrl map params_work_states",
    "flushState": "W7H property/post 2026-07-31; ctrl map params_work_states",
    "disinfectState": "W7H property/post 2026-07-31; ctrl map params_work_states",
    # D4SH (YumShare Dual-Hopper). Two independent sources, same as the W7H
    # rows: each is a JSON key in the state builder of a real D4SH 867 `ctrl`,
    # and each is present in BOTH real reports an owner posted in issue #2 —
    # one per transport, so neither is a quirk of one frame type.
    "food1": "D4SH 867 ctrl state builder; both reports in issue #2",
    "food2": "D4SH 867 ctrl state builder; both reports in issue #2",
    "door": "D4SH 867 ctrl state builder; both reports in issue #2",
    "bowl": "D4SH 867 ctrl 'recv feed start leftover set(-1)'; both reports in issue #2",
    "feeding": "D4SH 867 ctrl state builder; both reports in issue #2",
    "eating": "D4SH 867 ctrl state builder; both reports in issue #2",
    # D4H (YumShare Solo). Settled the question the D4SH rows above left open:
    # a single-hopper next-gen feeder sends the SINGULAR `food`, not `food1`.
    # Observed on a live D4H (fw 867): steady 2 with a full hopper, 0 when
    # nothing is detected (transiently during a dispense, for real when empty)
    # -- which is also why `food_low` reads the derived `foodLow`, never the
    # raw value (see state_parsers._derive_food_low).
    "food": "live D4H 867 state reports: steady 2 full, 0 empty/dispensing",
    # Values whose PRESENCE is settled and whose meaning is not. That is a fine
    # reason to publish -- an owner can watch a number move -- and no reason at
    # all to name it something, which is why each of these entities carries the
    # device's own word for it. See ha/entities/sensors.py.
    "ir_b_1": "D4SH 867 ctrl state builder; both reports in issue #2",
    "ir_b_2": "D4SH 867 ctrl state builder; both reports in issue #2",
    "ir_c": "D4SH 867 ctrl state builder; both reports in issue #2",
    "DCV": "D4SH 867 ctrl state builder; both reports in issue #2 (6228, 6234)",
    "left_hall": "D4SH 867 ctrl sensor{} block; both reports in issue #2",
    "home_hall": "D4SH 867 ctrl sensor{} block; both reports in issue #2",
    "right_hall": "D4SH 867 ctrl sensor{} block; both reports in issue #2",
    "left_sub_hall": "D4SH 867 ctrl sensor{} block; both reports in issue #2",
}

#: Pre-existing passthrough-only keys that were already published when the
#: check above was added. They are NOT evidence — they are debt, listed so the
#: guard can stop NEW ones without either blessing these silently or deleting
#: entities for hardware nobody here owns and nobody can test.
#:
#: Clearing an entry means one of two things: someone captures the field on real
#: hardware and it moves to PASSTHROUGH_ATTESTED, or it goes the way of
#: `sandLack` and friends. See the K2/K3 note in CLAUDE.md — both are BLE-only
#: today, so their WiFi-purifier entities cannot be exercised at all.
PASSTHROUGH_UNVERIFIED = {
    # `drinkTime` is back after a day out of this list. A real W7H does report
    # `device.drink_time`, but the map says it is the unix TIME OF THE LAST
    # DRINK, not a count — so it feeds the `last_drink` timestamp sensor, and
    # this key stays what it always was: the other fountains' cloud-model name
    # for a counter nobody here has seen on the wire.
    "filterLeftDays", "lackWarning", "heatRealTemp", "drinkTime",
    "desiccantLeftDays", "batteryPower",
    # `bowl`, `feeding` and `eating` graduated to PASSTHROUGH_ATTESTED once a
    # real D4SH sent all three, and `food` followed when a live D4H did. That
    # leaves `weight`: the same D4SH reports carry `food1`/`food2` and no
    # `weight`, and no feeder has sent one -- so it stays the reference
    # integration's cloud-model name, excluded on the models we can check.
    "weight",
    "liquid", "battery", "temp",
}


def _parser_for(device_type: str):
    """The `_parse_*` function `parse_state_report` dispatches this model to."""
    dispatch = {
        ("t5", "t6", "t7"): state_parsers._parse_litter_camera,
        ("t3", "t4"): state_parsers._parse_litter_esp32,
        ("d4h", "d4sh", "d4", "d3", "d4s", "feeder", "feedermini"): state_parsers._parse_feeder,
        ("w4", "w5", "ctw2", "ctw3", "w7h"): state_parsers._parse_water_fountain,
        ("k2", "k3"): state_parsers._parse_purifier,
    }
    for types, fn in dispatch.items():
        if device_type in types:
            return fn
    return None


def _keys_a_parser_can_emit(fn) -> set[str]:
    """Every string literal in a parser and the helpers it calls.

    A superset of what it can emit, which is the right direction: this test
    should fail only on an entity NOTHING could fill, never on one whose field
    merely depends on the payload.
    """
    keys: set[str] = set()
    for source in (fn, *_SHARED_HELPERS):
        tree = ast.parse(inspect.getsource(source).lstrip())
        keys |= {n.value for n in ast.walk(tree)
                 if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    # Names produced from a module-level table rather than a literal in the
    # function body, so the AST walk above cannot see them.
    keys |= set(state_parsers.PRESENCE_FLAGS.values())
    keys |= set(state_parsers.W7H_STATE_FIELDS)
    keys |= set(state_parsers.W7H_HALLS)
    keys |= set(state_parsers.W7H_DEVICE_TIMESTAMPS.values())
    keys |= set(state_parsers.LITTER_CAMERA_HALLS)
    keys |= set(state_parsers.FEEDER_NEXT_GEN_FIELDS)
    keys |= set(state_parsers.FEEDER_HALLS)
    return keys


#: Helpers every litter/feeder/fountain parser shares, scanned alongside it.
_SHARED_HELPERS = (
    state_parsers._extract_camel, state_parsers._extract_litter_nested,
    state_parsers._extract_consumable_days, state_parsers._extract_shared,
    state_parsers._extract_presence_flags, state_parsers._extract_error_flags,
    state_parsers._derive_food_low,
    state_parsers._extract_fountain_w7h,
    state_parsers._extract_wifi_rssi, state_parsers._extract_work_mode,
    state_parsers._parse_content_field, state_parsers.normalize_property_params,
)


def _passthrough_only_keys(fn) -> set[str]:
    """Names a parser can ONLY ever copy verbatim from the device payload.

    Two sets, and the difference is what matters:

    * *listed* — every string inside a list/tuple literal, i.e. the names handed
      to `_extract_camel` and the flat loop in `normalize_property_params`.
      Being here means "copy this if the device sends it", which says nothing
      about whether any device does.
    * *assigned* — names written through a real subscript assignment
      (``state["boxFull"] = ...``), i.e. a value this code derives or maps.

    A key that is only ever listed has no producer at all; a key that is also
    assigned does. `errorMsg` and `rssi` appear in both, which is why a blanket
    "is it in a passthrough list" check flags half the codebase.
    """
    listed: set[str] = set()
    assigned: set[str] = set()
    for source in (fn, *_SHARED_HELPERS):
        tree = ast.parse(inspect.getsource(source).lstrip())
        for node in ast.walk(tree):
            if isinstance(node, (ast.List, ast.Tuple)):
                listed |= {el.value for el in node.elts
                           if isinstance(el, ast.Constant) and isinstance(el.value, str)}
            elif isinstance(node, (ast.Assign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if (isinstance(t, ast.Subscript)
                            and isinstance(t.slice, ast.Constant)
                            and isinstance(t.slice.value, str)):
                        assigned.add(t.slice.value)
    # Passthrough tables that live at module level rather than inside the
    # function body, so the walk above cannot see them. They are handed
    # straight to `_extract_camel`/`_extract_sensor_block`, which makes every
    # name in them exactly what this function is looking for: listed, never
    # assigned.
    listed |= set(state_parsers.FEEDER_NEXT_GEN_FIELDS)
    listed |= set(state_parsers.FEEDER_HALLS)
    return listed - assigned


@pytest.mark.parametrize("device_type", sorted(DEVICE_TYPES_ALL))
def test_every_entity_a_model_publishes_has_something_that_can_fill_it(device_type):
    device = Device(petkit_id=1, device_type=device_type, serial_number="SN")
    entities = get_entities_for_device(device)
    if not entities:
        pytest.skip(f"{device_type} publishes no entities")

    parser = _parser_for(device_type)
    producible = _keys_a_parser_can_emit(parser) if parser else set()
    passthrough = _passthrough_only_keys(parser) if parser else set()
    seeded = set(defaults.default_settings(device))

    unbacked = []
    for e in entities:
        if not e.value_path:
            continue
        parts = e.value_path.split(".")
        root, leaf = parts[0], parts[-1]
        if root == "state":
            if leaf in RUNTIME_DERIVED:
                continue
            if leaf not in producible:
                unbacked.append(f"{e.component} {e.key} -> {e.value_path} (no parser produces it)")
            # Only top-level `state.<key>`: a nested path like
            # `state.feedState.times` is vouched for by its parent being built.
            elif (len(parts) == 2 and leaf in passthrough
                  and leaf not in PASSTHROUGH_ATTESTED
                  and leaf not in PASSTHROUGH_UNVERIFIED):
                # The hole this closes: being listed for copying is not evidence
                # any device sends it. Say where you saw it, or drop the entity.
                unbacked.append(
                    f"{e.component} {e.key} -> {e.value_path} (only a parser passthrough "
                    f"backs it — add {leaf!r} to PASSTHROUGH_ATTESTED with the capture, "
                    f"firmware string or reference model you saw it in)")
        elif root == "settings" and leaf not in seeded and leaf not in UNSEEDED_BY_DESIGN:
            unbacked.append(f"{e.component} {e.key} -> {e.value_path} (not in default_settings)")

    assert not unbacked, (
        f"{device_type} publishes entities nothing can fill:\n  " + "\n  ".join(unbacked)
        + "\n\nEither seed the setting in defaults.default_settings(), move the entity onto a "
          "capability-gated list, or remove it. An entity that can never hold a value is "
          "worse than a missing one."
    )


def test_the_passthrough_allowlist_does_not_outlive_its_entities():
    """An entry here exempts a key from needing a real producer, so a stale one
    silently re-opens the hole for whatever entity is added next under that
    name. Every entry must still be reachable from some model's parser."""
    reachable: set[str] = set()
    for device_type in DEVICE_TYPES_ALL:
        parser = _parser_for(device_type)
        if parser:
            reachable |= _passthrough_only_keys(parser)
    stale = sorted((set(PASSTHROUGH_ATTESTED) | PASSTHROUGH_UNVERIFIED) - reachable)
    assert not stale, f"attested but no parser passes them through any more: {stale}"

    overlap = sorted(set(PASSTHROUGH_ATTESTED) & PASSTHROUGH_UNVERIFIED)
    assert not overlap, (
        f"listed as both attested and unverified, so the evidence is ambiguous: {overlap}")


def test_the_runtime_derived_allowlist_stays_honest():
    """Each exemption above claims something writes it. If that stops being
    true the allow-list silently starts hiding real gaps, so check the two
    writers still exist."""
    from petkit_local.events.ingest import apply_derived_state
    from petkit_local.ha.publisher import HAPublisher

    src = inspect.getsource(apply_derived_state)
    for key in ("lastClean", "lastVisit", "lastFeed", "petWeight"):
        assert key in src, f"{key} is exempted but apply_derived_state no longer writes it"
    assert "lastClipPath" in inspect.getsource(HAPublisher.publish_media_ready)
    assert "streamUrl" in inspect.getsource(HAPublisher._build_state)


def test_no_entity_key_changed_when_a_label_did():
    """Entity keys are user state: renaming one orphans the live entity and
    loses its history. The Sand -> Litter pass renamed labels only.

    `sand_lack` used to be checked here too and is now deleted outright — a
    removal is honest (the entity could never hold a value), a rename is not.
    `total_time` is the live example of the rule: it is labelled "Uptime"
    because that is what the device reports, while the key stays `total_time`.
    """
    device = Device(petkit_id=1, device_type="t5", serial_number="SN")
    names = {e.key: e.name for e in get_entities_for_device(device)}
    assert "sand_saving" in names and "total_time" in names
    assert "Sand" not in names["sand_saving"]


def test_no_user_facing_label_still_says_sand():
    """The device stores `sandWeight`/`sandPercent` on the wire, but nothing a
    user reads should say "sand" — the product is litter.

    Covers the event code table as well as the entity names. The first pass at
    this rename only checked `EntityDef.name` and so missed four labels in
    `events/codes.py`, which render straight onto Timeline cards.
    """
    from petkit_local.events import codes

    for device_type in ("t3", "t5"):
        device = Device(petkit_id=1, device_type=device_type, serial_number="SN")
        offenders = [e.key for e in get_entities_for_device(device) if "sand" in e.name.lower()]
        assert not offenders, f"entity labels: {offenders}"

    bad = []
    for key, code in codes.ALL_EVENT_CODES.items():
        for field in (code.label, code.done_word):
            if "sand" in (field or "").lower():
                bad.append(f"{key}: {field!r}")
    assert not bad, f"event labels a user reads: {bad}"
