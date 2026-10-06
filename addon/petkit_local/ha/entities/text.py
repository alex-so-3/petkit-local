"""HA `text` entities: raw schedule JSON, and do-not-disturb windows.

The value is the JSON the device fetches via dev_schedule_get / dev_feed_get.
Editing it in HA writes to device.config[<key>], which those handlers serve.
UX is intentionally basic (a JSON string) — it makes schedules editable
end-to-end without a bespoke UI. Note HA's 255-character cap on a text entity
(enforced in `ha/discovery.py`): a longer schedule has to be edited in the web
panel instead.

The do-not-disturb windows are the exception to "raw JSON": a `*MultiRange` is
shown and typed as `HH:MM-HH:MM`, comma separated for several
(`ha/commands.py::format_ranges`/`parse_ranges`).

`ha/categories.py` decides which of these lists a given device type gets.
"""
from petkit_local.ha.discovery import EntityDef

LITTER_SCHEDULE_TEXT = [
    EntityDef(component="text", key="cleaning_schedule", name="Cleaning Schedule (JSON)",
              value_path="schedule", icon="mdi:calendar-clock",
              entity_category="config"),
]

FEEDER_SCHEDULE_TEXT = [
    EntityDef(component="text", key="feeding_schedule", name="Feeding Schedule (JSON)",
              value_path="feed_schedule", icon="mdi:calendar-clock",
              entity_category="config"),
]

#: The W7H's three do-not-disturb WINDOWS, next to the switches that turn each
#: one on (`refill_disturb_mode`, `water_level_disturb_mode`,
#: `voice_disturb_mode`).
#:
#: The official app sets each as `property.set` with the JSON-string shape of
#: every range field — `"{\"awDisturbMultiRange\":[[0,600]]}"`, minutes since
#: local midnight (capture 2026-10-04, `codes.FOUNTAIN_W7H_APP_SET_FIELDS`). An
#: HA `time` entity holds one moment and a window is two, so a pair of them
#: would leave half a window set between two writes; one text field is atomic
#: and also carries the several ranges the app's model allows. `19:00-11:00`
#: crosses midnight, as the captured `[[1140, 660]]` does.
#:
#: `value_path` is `multi_config.<field>`: the same store `dev_multi_config` is
#: served from and the web panel's schedule editor writes, so all three show the
#: same window. Not `settings.`: `to_device_info` would serve it a second time,
#: in a payload the real cloud never puts it in.
FOUNTAIN_W7H_RANGE_TEXT = [
    EntityDef(component="text", key="refill_disturb_period", name="Quiet Refill Hours",
              value_path="multi_config.awDisturbMultiRange", icon="mdi:clock-time-eight",
              entity_category="config"),
    EntityDef(component="text", key="signal_lights_disturb_period",
              name="Quiet Signal Lights Hours",
              value_path="multi_config.wlDisturbMultiRange", icon="mdi:clock-time-eight",
              entity_category="config"),
    EntityDef(component="text", key="voice_disturb_period", name="Quiet Voice Prompts Hours",
              value_path="multi_config.toneMultiRange", icon="mdi:clock-time-eight",
              entity_category="config"),
]
