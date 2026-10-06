"""HA `number` entities — the numeric device settings, per device category.

Each one reads `settings.<field>` from the state document and writes the same
field back via `property.set`. `min_value`/`max_value`/`step` are HA-side
validation only: the device does its own clamping, so these bounds exist to
stop an obviously-wrong value from ever being sent, not to guarantee one.

`ha/categories.py` decides which of these lists a given device type gets.
"""
from petkit_local.ha.discovery import EntityDef

LITTER_NUMBERS = [
    EntityDef(component="number", key="cleaning_delay", name="Cleaning Delay",
              value_path="settings.stillTime", icon="mdi:timer-sand",
              unit="s", min_value=0, max_value=3600, step=60),
]

FEEDER_NUMBERS = [
]

#: Seeded by `defaults.default_settings()` only inside its `is_camera` branch,
#: so on a non-camera model these render blank forever. Two sources agree
#: they belong to the camera hardware: the defaults table and the state
#: parsers (`_parse_litter_camera` is documented as "the ESP32 litter set
#: PLUS the camera, spray and package fields").
#:
#: THE VOLUME RANGE IS STILL UNVERIFIED at its ends, and 0-9 should not be read
#: as evidence. The field itself is real — PetKit's own cloud sends `volume` in
#: `dev_device_info` — and two sources now agree on 1..9: localkit's validator
#: for the YumShare Solo, a FEEDER (`PetkitYumshareSolo.php`, whose picker
#: offers 1-9, not 0-9), and a capture-derived T6 map that reports an "observed
#: range including 1 through 9". Neither establishes 0, and neither establishes
#: that 9 is the ceiling rather than the highest anybody happened to pick; our
#: own T5 currently sits at 9. So the bound stays as it is, deliberately one
#: step wider at the bottom, and the panel shows it to whoever hits it.
LITTER_CAMERA_NUMBERS = [
    EntityDef(component="number", key="volume", name="Volume",
              value_path="settings.volume", icon="mdi:volume-high",
              min_value=0, max_value=9, step=1),
]

#: How much each hopper of a Dual-Hopper dispenses per press.
#:
#: `local.` and not `settings.`, which is the whole point of that prefix: this
#: is our intent, not a device setting. `payloads.to_device_info` serves
#: `config["settings"]` straight back to the device, so a value parked there
#: would be pushed to the feeder as a setting it never had.
#:
#: The unit really is portions on this model — its firmware reads `amount1`
#: verbatim with no scaling, and PetKit's app sends 1 for a single portion
#: (issue #2). That is NOT true of the single-hopper `amount`, which the device
#: divides before use, which is why these entities exist for the dual model
#: alone.
#:
#: 1..10 is a soft bound. The only limit the firmware evidences is its own
#: single-byte store (`sb`), so 255 is where a value would start wrapping;
#: 10 is a sane ceiling for a control someone taps, not a measured maximum.
FEEDER_DUAL_NUMBERS = [
    EntityDef(component="number", key="hopper1_portions", name="Hopper 1 Portions",
              value_path="local.feedAmount1", icon="mdi:silverware-fork-knife",
              min_value=0, max_value=10, step=1),
    EntityDef(component="number", key="hopper2_portions", name="Hopper 2 Portions",
              value_path="local.feedAmount2", icon="mdi:silverware-fork-knife",
              min_value=0, max_value=10, step=1),
]

FEEDER_CAMERA_NUMBERS = [
    EntityDef(component="number", key="volume", name="Volume",
              value_path="settings.volume", icon="mdi:volume-high",
              min_value=0, max_value=9, step=1),
    EntityDef(component="number", key="selected_sound", name="Selected Sound",
              value_path="settings.selectedSound", icon="mdi:music-note",
              min_value=-1, max_value=10, step=1,
              entity_category="config"),
]

FOUNTAIN_NUMBERS = [
    EntityDef(component="number", key="fountain_time", name="Fountain Time",
              value_path="settings.fountainTime", icon="mdi:clock-outline",
              unit="h", min_value=1, max_value=24, step=1),
    EntityDef(component="number", key="sleep_time", name="Sleep Time",
              value_path="settings.sleepTime", icon="mdi:sleep",
              unit="h", min_value=1, max_value=24, step=1),
]

#: How often the W7H runs each of its two water-treatment cycles, in DAYS.
#:
#: `switches.py` explains why these were withheld until now: a number needs a
#: range, and the firmware map that named the handlers gave none. The
#: capture-derived map supplied 2026-08-09 does — "N = every N days", with 1 and
#: 7 both observed — which is the encoding, not the bound.
#:
#: 1..7 is the official app's own range: both pickers ("Drain & Refill" and
#: "Drain & Flush") offer 1-7 days, read off the app driving a W7H on fw 456
#: (2026-10-04). It was 1..30 here, a soft ceiling guessed before anybody had
#: looked, which let HA send values the app can never produce.
#:
#: The matching times are `time` entities, not numbers — see `times.py`. The
#: app capture of 2026-10-04 confirms the encoding (`waterChangeCycle` 4).
FOUNTAIN_W7H_NUMBERS = [
    EntityDef(component="number", key="water_change_cycle", name="Drain & Refill Cycle",
              value_path="settings.waterChangeCycle", icon="mdi:water-refresh",
              unit="d", min_value=1, max_value=7, step=1),
    EntityDef(component="number", key="flush_cycle", name="Drain & Flush Cycle",
              value_path="settings.flushCycle", icon="mdi:water-sync",
              unit="d", min_value=1, max_value=7, step=1),
    # 1-9, the official app's slider (captured at 1, 9 and 2 on 2026-10-04,
    # `codes.FOUNTAIN_W7H_APP_SET_FIELDS`). It was 0-9 here, one step wider than
    # anything the app can send; the litter and feeder lists keep their own
    # bound, since this capture says nothing about them.
    EntityDef(component="number", key="volume", name="Volume",
              value_path="settings.volume", icon="mdi:volume-high",
              min_value=1, max_value=9, step=1),
    # MINUTES, 1-60, and they override the family's `fountain_time` and
    # `sleep_time` in place (same keys, so the same HA entities). The family
    # list says hours 1-24, a cloud-model guess for the Bluetooth fountains; the
    # official app's pickers for a W7H run 1-60 min and it was captured writing
    # 15, 28 and 60 (`codes.FOUNTAIN_W7H_APP_SET_FIELDS`). Both only matter in
    # the intermittent flow mode: how long the water runs, then how long it
    # rests.
    EntityDef(component="number", key="fountain_time", name="Fountain Time",
              value_path="settings.fountainTime", icon="mdi:clock-outline",
              unit="min", min_value=1, max_value=60, step=1),
    EntityDef(component="number", key="sleep_time", name="Sleep Time",
              value_path="settings.sleepTime", icon="mdi:sleep",
              unit="min", min_value=1, max_value=60, step=1),
]
