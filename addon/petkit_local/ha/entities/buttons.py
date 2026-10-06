"""HA `button` entities for litter boxes, feeders and fountains.

A button is the one entity kind with no state: pressing it publishes its `key`
and nothing else. That key is the contract with `ha/commands.py::ALL_ACTIONS`,
which owns the actual device action codes — a button whose key has no entry
there appears in HA and does nothing but log a warning, so the two lists must
be kept in step.

`ha/categories.py` decides which of these lists a given device type gets.
"""
from petkit_local.ha.discovery import EntityDef

LITTER_BUTTONS = [
    EntityDef(component="button", key="cleaning_start", name="Scoop", icon="mdi:broom"),
    EntityDef(component="button", key="maintenance_start", name="Enter Maintenance", icon="mdi:wrench"),
    EntityDef(component="button", key="maintenance_stop", name="Exit Maintenance", icon="mdi:wrench-check"),
    EntityDef(component="button", key="dump_litter", name="Dump Litter", icon="mdi:delete-sweep"),
    EntityDef(component="button", key="deodorize", name="Deodorize", icon="mdi:spray"),
    EntityDef(component="button", key="reset_n50", name="Reset N50", icon="mdi:restart"),
    EntityDef(component="button", key="reset_n60", name="Reset N60", icon="mdi:restart"),
    EntityDef(component="button", key="pause", name="Pause", icon="mdi:pause"),
    EntityDef(component="button", key="resume", name="Resume", icon="mdi:play"),
    EntityDef(component="button", key="reset", name="Reset", icon="mdi:stop"),
    EntityDef(component="button", key="level_litter", name="Level Litter", icon="mdi:format-align-bottom"),
]

#: Litter-box actions that need the camera generation's hardware.
#:
#: `light` is the illuminator: LBCommand calls 7 LIGHT and three isolated taps
#: of the app's Light action on a T6 each emitted `start_action: 7`, so two
#: independent sources agree. It sits here rather than on the shared list
#: because the illuminator is part of the camera assembly.
#:
#: Power is a SERVICE OF ITS OWN — `thing.service.power`, not `property.set`.
#: The distinction matters: two fountain buttons were removed for writing
#: `power` as a setting to a field no firmware reads, and the fix is a different
#: service, not a different field. See `ha/commands.py::_device_power`.
LITTER_CAMERA_BUTTONS = [
    EntityDef(component="button", key="light", name="Light", icon="mdi:lightbulb-on-outline"),
    EntityDef(component="button", key="power_off", name="Power Off", icon="mdi:power-off"),
    EntityDef(component="button", key="power_on", name="Power On", icon="mdi:power-on"),
]

#: Purobot Ultra only.
#:
#: Both come from single isolated taps in the app's action sheet on a T6, and
#: neither is safe to extend to its siblings:
#:
#: * `pack_waste` sends `start_action: 8` — the value pypetkitapi calls
#:   RESET_N50_DEODOR. Those two readings cannot both be right, and the T6 one
#:   is what a controlled tap produced. This box has no N50 cartridge, so its
#:   `reset_n50` button is excluded in `ha/categories.py` and the code goes
#:   here under the name the hardware gives it.
#: * `open_sealed_door` sends `start_action: 11`. The sealed waste door is this
#:   model's hardware; on a box without one, 11 is an unknown value.
LITTER_T6_BUTTONS = [
    EntityDef(component="button", key="pack_waste", name="Pack Waste", icon="mdi:package-down"),
    EntityDef(component="button", key="open_sealed_door", name="Open Sealed Door",
              icon="mdi:door-open"),
]

FEEDER_BUTTONS = [
    EntityDef(component="button", key="feed", name="Feed", icon="mdi:food"),
    EntityDef(component="button", key="reset_desiccant", name="Reset Desiccant", icon="mdi:restart"),
    EntityDef(component="button", key="cancel_manual_feed", name="Cancel Manual Feed", icon="mdi:cancel"),
    EntityDef(component="button", key="food_replenished", name="Food Replenished", icon="mdi:food-apple"),
]

#: Camera feeder buttons. `play_sound` plays the currently selected custom
#: sound on the device; the sound must be uploaded and selected first (see
#: `ha/entities/numbers.py::FEEDER_CAMERA_NUMBERS` for `selectedSound`).
FEEDER_CAMERA_BUTTONS = [
    EntityDef(component="button", key="play_sound", name="Play Sound",
              icon="mdi:play-circle", entity_category="config"),
]

#: A Dual-Hopper only. `feed` above already dispenses from both, so these are
#: the one thing it cannot express: food from one hopper and not the other.
#: PetKit's app does exactly this, and does it through the same service rather
#: than a different one — its single-hopper feed was captured as
#: `{"amount1": 0, "amount2": 1}` (issue #2). How much each dispenses is the
#: matching `number` in `numbers.py::FEEDER_DUAL_NUMBERS`.
FEEDER_DUAL_BUTTONS = [
    EntityDef(component="button", key="feed_hopper_1", name="Feed Hopper 1",
              icon="mdi:food"),
    EntityDef(component="button", key="feed_hopper_2", name="Feed Hopper 2",
              icon="mdi:food"),
]

FOUNTAIN_BUTTONS = [
    EntityDef(component="button", key="reset_filter", name="Reset Filter", icon="mdi:filter-remove"),
    EntityDef(component="button", key="pause_fountain", name="Pause", icon="mdi:pause"),
    EntityDef(component="button", key="resume_fountain", name="Resume", icon="mdi:play"),
]

#: The W7H's water-treatment jobs, as `thing.service.start` actions.
#:
#: Exactly the three the official app offers, with the values it sends
#: (`codes.FOUNTAIN_W7H_APP_ACTIONS`, from a capture of the app on 2026-10-04):
#: Drain & flush is 1, Refill is 2, Drain is 3. After a Drain the FlowLift
#: module stays raised until a Refill lowers it again — the app says so in as
#: many words, so a Drain is half of a job, not a whole one.
#:
#: Three buttons this list used to carry are gone, and their keys must not come
#: back (`ha/categories.py` retires their discovery topics):
#:
#:   * `fountain_water_change` sent `start_action: 5`. The app has no such
#:     control, and on a real W7H it crashed `ctrl` three times out of three
#:     (`codes.FOUNTAIN_W7H_START_ACTIONS_CRASH`).
#:   * `power_off`/`power_on`. `power` is a service the firmware accepts, but no
#:     screen of the app offers it, and a power-off is not a command to guess at.
#:
#: Deep clean is deliberately absent too. The cycle exists (`ster_mode_*.aac`,
#: the `disinfectState` field, "Boil ster over result" in the log) but no
#: accepted action value can be tied to it, and it is the one job that needs a
#: person standing there with a kettle.
FOUNTAIN_W7H_BUTTONS = [
    EntityDef(component="button", key="fountain_flush", name="Drain & Flush",
              icon="mdi:water-sync"),
    EntityDef(component="button", key="fountain_refill", name="Refill",
              icon="mdi:water-plus"),
    EntityDef(component="button", key="fountain_drain", name="Drain",
              icon="mdi:water-minus"),
]
