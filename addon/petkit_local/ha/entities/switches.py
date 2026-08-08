"""HA `switch` entities — the boolean device settings, per device category.

Almost every entry maps to `settings.<field>`: HA's ON/OFF becomes the 0/1 the
device stores, written through `property.set`. The `*_CAMERA_SWITCHES` lists
are the extra settings that only exist on camera-equipped hardware and are
added on top of the base list for those device types.

`CAPABILITY_SWITCHES` is the exception and is documented at its definition —
it does not write device settings at all.

`devices/categories.py` decides which of these lists a given device type gets.
"""
from petkit_local.ha.discovery import EntityDef

LITTER_SWITCHES = [
    EntityDef(component="switch", key="auto_work", name="Auto Clean",
              value_path="settings.autoWork", icon="mdi:auto-fix"),
    EntityDef(component="switch", key="child_lock", name="Child Lock",
              value_path="settings.manualLock", icon="mdi:lock"),
    EntityDef(component="switch", key="display", name="Display",
              value_path="settings.lightMode", icon="mdi:monitor"),
    EntityDef(component="switch", key="avoid_repeat", name="Avoid Repeat Clean",
              value_path="settings.avoidRepeat", icon="mdi:repeat-off"),
    EntityDef(component="switch", key="kitten", name="Kitten Mode",
              value_path="settings.kitten", icon="mdi:cat"),
    EntityDef(component="switch", key="disturb_mode", name="Do Not Disturb",
              value_path="settings.disturbMode", icon="mdi:bell-off"),
    EntityDef(component="switch", key="deep_clean", name="Deep Cleaning",
              value_path="settings.deepClean", icon="mdi:broom"),
    EntityDef(component="switch", key="underweight", name="Underweight Detection",
              value_path="settings.underweight", icon="mdi:weight"),
    EntityDef(component="switch", key="bury", name="Waste Covering",
              value_path="settings.bury", icon="mdi:shovel"),
    EntityDef(component="switch", key="downpos", name="Continuous Rotation",
              value_path="settings.downpos", icon="mdi:rotate-right"),
    # `key` stays `sand_saving`: it is the entity's identity in every existing
    # installation, and renaming it would orphan the entity and lose its
    # history. Only the label follows the device's own wording.
    EntityDef(component="switch", key="sand_saving", name="Litter Saving",
              value_path="settings.sandSaving", icon="mdi:leaf"),
    EntityDef(component="switch", key="fixed_time_clear", name="Periodic Cleaning",
              value_path="settings.fixedTimeClear", icon="mdi:clock-outline"),
]

#: Seeded by `Device.default_settings()` only inside its `is_camera` branch,
#: so on a non-camera model these render blank forever. Two sources agree
#: they belong to the camera hardware: the defaults table and the state
#: parsers (`_parse_litter_camera` is documented as "the ESP32 litter set
#: PLUS the camera, spray and package fields").
LITTER_CAMERA_SWITCHES = [
    EntityDef(component="switch", key="auto_spray", name="Auto Deodorizing",
              value_path="settings.autoSpray", icon="mdi:spray"),
    EntityDef(component="switch", key="fixed_time_spray", name="Periodic Deodorizing",
              value_path="settings.fixedTimeSpray", icon="mdi:clock-outline"),
    EntityDef(component="switch", key="deep_spray", name="Deep Deodorizing",
              value_path="settings.deepSpray", icon="mdi:spray-bottle"),

    EntityDef(component="switch", key="camera", name="Camera",
              value_path="settings.camera", icon="mdi:camera"),
    EntityDef(component="switch", key="microphone", name="Microphone",
              value_path="settings.microphone", icon="mdi:microphone"),
    EntityDef(component="switch", key="night_vision", name="Night Vision",
              value_path="settings.night", icon="mdi:weather-night"),
    EntityDef(component="switch", key="light_assist", name="Light Assist",
              value_path="settings.lightAssist", icon="mdi:lightbulb-on"),
    EntityDef(component="switch", key="toilet_light", name="Toilet Light",
              value_path="settings.toiletLight", icon="mdi:lightbulb"),
    EntityDef(component="switch", key="camera_light", name="Camera Light",
              value_path="settings.cameraLight", icon="mdi:flashlight"),
    EntityDef(component="switch", key="video_timestamp", name="Video Timestamp",
              value_path="settings.timeDisplay", icon="mdi:clock-digital"),
    # The two AI-camera detections PetKit's app exposes as their own screens,
    # both marked Beta there. Field names confirmed against a captured
    # dev_device_info (2026-07-27) whose values matched the app's switch
    # positions, each corroborated by the event content it produces:
    # `voice` <-> petVoice/voice_time on a visit summary, `phDetection` <->
    # ph_reason on a cleaning. See events/decode.py for those.
    EntityDef(component="switch", key="yowling_detection", name="Yowling Detection",
              value_path="settings.voice", icon="mdi:ear-hearing"),
    EntityDef(component="switch", key="ph_detection", name="Urine pH Detection",
              value_path="settings.phDetection", icon="mdi:test-tube"),
]

FEEDER_SWITCHES = [
    EntityDef(component="switch", key="child_lock", name="Child Lock",
              value_path="settings.manualLock", icon="mdi:lock"),
    EntityDef(component="switch", key="food_warn", name="Shortage Alarm",
              value_path="settings.foodWarn", icon="mdi:alert"),
    EntityDef(component="switch", key="indicator_light", name="Indicator Light",
              value_path="settings.lightMode", icon="mdi:lightbulb"),
    # Confirmed present in real D4SH firmware (`feedSound` appears in `ctrl`
    # and `libbase.so`), but deliberately NOT seeded in
    # `Device.default_settings()`: `to_device_info` serves the seeded settings
    # straight back to the device, so inventing a value here would silently
    # change the owner's setting. It stays unknown until the device reports it.
    EntityDef(component="switch", key="feed_sound", name="Feed Sound",
              value_path="settings.feedSound", icon="mdi:bell-ring"),
]
# REMOVED (2026-07-29): `feed_tone` (settings.feedTone) and `disturb_mode`
# (settings.disturbMode). Both came from the reference integration, which models
# PetKit's CLOUD API rather than the device protocol, and NEITHER string appears
# anywhere in real D4SH firmware — while every field kept above does. They were
# published to every feeder and could never hold a value.

FEEDER_CAMERA_SWITCHES = [
    EntityDef(component="switch", key="voice_dispense", name="Voice Dispense",
              value_path="settings.soundEnable", icon="mdi:account-voice"),

    EntityDef(component="switch", key="camera", name="Camera",
              value_path="settings.camera", icon="mdi:camera"),
    EntityDef(component="switch", key="microphone", name="Microphone",
              value_path="settings.microphone", icon="mdi:microphone"),
    EntityDef(component="switch", key="night_vision", name="Night Vision",
              value_path="settings.night", icon="mdi:weather-night"),
    EntityDef(component="switch", key="time_display", name="Time Display",
              value_path="settings.timeDisplay", icon="mdi:clock-outline"),
    EntityDef(component="switch", key="move_detection", name="Move Detection",
              value_path="settings.moveDetection", icon="mdi:motion-sensor"),
    EntityDef(component="switch", key="pet_detection", name="Pet Detection",
              value_path="settings.petDetection", icon="mdi:cat"),
    EntityDef(component="switch", key="eat_detection", name="Eat Detection",
              value_path="settings.eatDetection", icon="mdi:food"),
    EntityDef(component="switch", key="smart_frame", name="Smart Frame",
              value_path="settings.smartFrame", icon="mdi:crop"),
]

FOUNTAIN_SWITCHES = [
    EntityDef(component="switch", key="child_lock", name="Child Lock",
              value_path="settings.manualLock", icon="mdi:lock"),
    EntityDef(component="switch", key="indicator_light", name="Indicator Light",
              value_path="settings.lightMode", icon="mdi:lightbulb"),
    EntityDef(component="switch", key="disturb_mode", name="Do Not Disturb",
              value_path="settings.disturbMode", icon="mdi:bell-off"),
    EntityDef(component="switch", key="auto_refill", name="Auto Refill",
              value_path="settings.addWaterSwitch", icon="mdi:water-plus"),
    EntityDef(component="switch", key="pet_detection", name="Pet Detection",
              value_path="settings.petDetection", icon="mdi:cat"),
    EntityDef(component="switch", key="heater", name="Heater",
              value_path="settings.heaterSwitch", icon="mdi:thermometer"),
]

# --- W7H (EverSweet Ultra AI) ----------------------------------------------
# Every field below is a wire name the W7H's own `ctrl` dispatches a set
# handler for, from the reverse-engineered settings map supplied 2026-07-31.
# That map is authoritative for the NAME and for the fact that the device
# accepts a write at all; it does NOT state a type. So this list stops at the
# fields whose handler or family makes a 0/1 unambiguous — `drinkDetection` is
# handled by `drinkDetection_Enable`, exactly as the `petDetection` switch we
# already ship is handled by `pet_det_Enable`.
#
# Deliberately NOT here: `flushIntensity`, `flushCycle`, `flushTime`,
# `waterChangeCycle`, `waterChangeTime`, `addWaterMode` and `heaterTemp`. All
# are real handlers, but a number or select needs a RANGE and the map gives
# none, and `Device.to_device_info` serves settings back to the device — so an
# invented bound is not a display detail, it is a value we would push.
#
# None of these are seeded in `devices/base.py` for the same reason: the honest
# state is unknown until the device reports one. See `UNSEEDED_BY_DESIGN` in
# tests/test_entity_backing.py.
FOUNTAIN_W7H_SWITCHES = [
    EntityDef(component="switch", key="drink_detection", name="Drink Detection",
              value_path="settings.drinkDetection", icon="mdi:cup-water"),
    EntityDef(component="switch", key="vomit_detection", name="Vomit Detection",
              value_path="settings.vomitDetection", icon="mdi:emoticon-sick"),
    EntityDef(component="switch", key="auto_flush", name="Auto Flush",
              value_path="settings.autoFlush", icon="mdi:water-sync"),
    EntityDef(component="switch", key="auto_water_change", name="Auto Water Change",
              value_path="settings.autoWaterChange", icon="mdi:water-refresh"),
    # Status-light toggles, siblings of the `indicator_light` switch this file
    # already ships for `lightMode`.
    EntityDef(component="switch", key="clean_water_lack_light", name="Low Water Light",
              value_path="settings.cleanWaterLackLight", icon="mdi:lightbulb-alert",
              entity_category="config"),
    EntityDef(component="switch", key="clean_water_empty_light", name="Empty Water Light",
              value_path="settings.cleanWaterEmptyLight", icon="mdi:lightbulb-alert",
              entity_category="config"),
    EntityDef(component="switch", key="waste_water_full_light", name="Waste Full Light",
              value_path="settings.wasteWaterFullLight", icon="mdi:lightbulb-alert",
              entity_category="config"),
    EntityDef(component="switch", key="wifi_light_assist", name="WiFi Status Light",
              value_path="settings.wifiLightAssist", icon="mdi:wifi",
              entity_category="config"),
    # Do-not-disturb for two specific subsystems, alongside the general
    # `disturb_mode` switch above.
    EntityDef(component="switch", key="refill_disturb_mode", name="Quiet Refill",
              value_path="settings.awDisturbMode", icon="mdi:bell-off",
              entity_category="config"),
    EntityDef(component="switch", key="water_level_disturb_mode", name="Quiet Water Level Alerts",
              value_path="settings.wlDisturbMode", icon="mdi:bell-off",
              entity_category="config"),
]

# Media upload capability toggles — mirror devices/base.py::Device.
# CAPABILITY_TYPES. Shared across all camera-capable categories (litter/
# feeder/fountain), gated on device.is_camera the same way the camera entity
# is (see ha/entities/camera.py). Routed specially in ha/commands.py — no
# device push, the STS response is the control point.
CAPABILITY_SWITCHES = [
    EntityDef(component="switch", key="capability_full_video", name="Recordings",
              value_path="capabilities.fullVideo", icon="mdi:video",
              entity_category="config"),
    EntityDef(component="switch", key="capability_event_image", name="Snapshots",
              value_path="capabilities.eventImage", icon="mdi:image-multiple",
              entity_category="config"),
    EntityDef(component="switch", key="capability_high_light", name="Highlights",
              value_path="capabilities.highLight", icon="mdi:star",
              entity_category="config"),
    EntityDef(component="switch", key="capability_dynamic_video", name="Motion Clips",
              value_path="capabilities.dynamicVideo", icon="mdi:motion-play",
              entity_category="config"),
]

PURIFIER_SWITCHES = [
    EntityDef(component="switch", key="indicator_light", name="Indicator Light",
              value_path="settings.lightMode", icon="mdi:lightbulb"),
    EntityDef(component="switch", key="child_lock", name="Child Lock",
              value_path="settings.manualLock", icon="mdi:lock"),
    EntityDef(component="switch", key="sound", name="Sound",
              value_path="settings.sound", icon="mdi:volume-high"),
]
