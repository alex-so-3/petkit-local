"""HA `sensor` and `binary_sensor` entities for every device category.

These are the read-only half of the integration: each entry names a key under
`state.` in the document `ha/publisher.py::_build_state` publishes, which
`devices/state_parsers.py` fills from HTTP state reports and MQTT property
posts. Nothing here is settable — the writable side lives in switches.py,
numbers.py, selects.py and text.py.

A `value_path` pointing at a key a device *may* not report is fine: the template
defaults it and the entity reads empty until the device sends one, which is what
lets one list serve several firmware versions.

That is NOT a licence to publish an entity nothing can ever fill. A field no
state parser produces and no default seeds reads unknown forever, which a user
cannot tell apart from a device that has not reported yet — four such entities
shipped that way and were removed (see the note further down). The rule is
enforced by `tests/test_entity_backing.py`, which walks every codename.

`ha/categories.py` decides which of these lists a given device type gets.
"""
from petkit_local.devices.state_parsers import WORK_MODE_IDLE
from petkit_local.events import codes
from petkit_local.ha.discovery import EntityDef

#: The work-mode enum, as labels+values for the value template. Imported rather
#: than restated: `events/codes.py` is the single source for protocol tables,
#: and a second copy here is exactly how a table drifts. Sorted so the pairing
#: is stable across runs.
#:
#: `WORK_MODE_IDLE` is prepended because it is NOT one of the device's codes —
#: a box reports no `workState` at all while idle, and the parser turns that
#: absence into this sentinel. It belongs in the rendering, not in the protocol
#: table. An unmapped mode still renders as its raw number rather than blank
#: (see `_enum_sensor_value_template`), so a code we have not seen stays visible.
_WORK_MODE_VALUES = [WORK_MODE_IDLE, *sorted(codes.WORK_MODES)]
_WORK_MODE_LABELS = ["idle", *(codes.WORK_MODES[v] for v in sorted(codes.WORK_MODES))]

LITTER_SENSORS = [
    EntityDef(component="sensor", key="device_status", name="Device Status",
              value_path="state.workingState", icon="mdi:state-machine",
              options=_WORK_MODE_LABELS, option_values=_WORK_MODE_VALUES),
    EntityDef(component="sensor", key="error", name="Error",
              value_path="state.errorMsg", icon="mdi:alert-circle"),
    # GRAMS, not kilograms. The device reports `litter.weight` as an integer
    # gram count (5469 in real T5 reports), `pet_weight` below uses `g` for the
    # same magnitude, and the panel's Timeline divides by 1000 to render kg.
    # This declared `kg` for a while, so HA was told the box held 5469 kg.
    EntityDef(component="sensor", key="litter_weight", name="Litter Weight",
              value_path="state.sandWeight", device_class="weight", unit="g"),
    EntityDef(component="sensor", key="litter_percent", name="Litter Level",
              value_path="state.sandPercent", unit="%", icon="mdi:percent"),
    EntityDef(component="sensor", key="used_times", name="Times Used",
              value_path="state.usedTimes", icon="mdi:counter"),
    EntityDef(component="sensor", key="n50_durability", name="N50 Days Left",
              value_path="state.deodorantLeftDays", unit="days", icon="mdi:air-filter"),
    EntityDef(component="sensor", key="n60_spray_days", name="N60 Spray Days Left",
              value_path="state.sprayLeftDays", unit="days", icon="mdi:spray"),
    # UPTIME, despite the key. `state.totalTime` is assigned straight from the
    # report's `runtime` (state_parsers._extract_litter_nested), which is how
    # long the device has been powered — on a live T5 it equalled `runtime` and
    # tracked `ble_os_run_ms` exactly. It was named "Total Usage Time", which
    # reads as cumulative time cats have spent in the box. `key` stays
    # `total_time` because renaming it would orphan the live entity.
    EntityDef(component="sensor", key="total_time", name="Uptime",
              value_path="state.totalTime", unit="s", device_class="duration",
              icon="mdi:timer", entity_category="diagnostic"),
    EntityDef(component="sensor", key="rssi", name="WiFi Signal",
              value_path="state.rssi", device_class="signal_strength", unit="dBm"),
    EntityDef(component="sensor", key="pet_weight", name="Pet Weight",
              value_path="state.petWeight", device_class="weight", unit="g",
              icon="mdi:scale-bathroom", entity_category="diagnostic"),
    EntityDef(component="sensor", key="last_clean", name="Last Cleaned",
              value_path="state.lastClean", device_class="timestamp",
              icon="mdi:broom", entity_category="diagnostic"),
    EntityDef(component="sensor", key="last_visit", name="Last Visit",
              value_path="state.lastVisit", device_class="timestamp",
              icon="mdi:cat", entity_category="diagnostic"),
]

LITTER_BINARY_SENSORS = [
    EntityDef(component="binary_sensor", key="waste_bin", name="Waste Bin Full",
              value_path="state.boxFull", device_class="problem", icon="mdi:delete-variant"),
    EntityDef(component="binary_sensor", key="pet_occupied", name="Toilet Occupied",
              value_path="state.petInTime", device_class="occupancy", icon="mdi:cat"),
    EntityDef(component="binary_sensor", key="waste_bin_present", name="Waste Bin Installed",
              value_path="state.boxState", icon="mdi:delete-empty"),
]
# REMOVED (2026-07-30): `sand_lack` (state.sandLack), `weight_error`
# (state.petError), `frequent_use` (state.frequentRestroom), `low_power`
# (state.lowPower) and `litter_tray` (state.sandTrayState).
#
# Same failure and the same evidence as the two removed below. Each name was
# searched for in three independent places and found in none:
#   * real T5 `ctrl` — absent (while `sprayState`, `boxState` and
#     `refreshState`, which the device really does send, are all present);
#   * every capture file, ~90k lines including 12,115 of real PetKit cloud
#     traffic and 905 state reports — zero occurrences of any of the five;
#   * a live T5's 54 reported state keys — absent.
# They survived only because `tests/test_entity_backing.py` accepted a name
# listed in a parser's passthrough tuple as proof something could fill it,
# which proves nothing at all. That hole is closed in the same commit.

#: The deodorizer state a camera-equipped litter box reports. Both fields are
#: in real T5 firmware, and `_parse_litter_esp32` states that T3/T4 "have no
#: camera or spray fields" — its extract list omits both — so on those models
#: they could only ever read unknown.
LITTER_CAMERA_SENSORS = [
    EntityDef(component="binary_sensor", key="deodorizer_present", name="N60 Deodorizer Present",
              value_path="state.sprayState", icon="mdi:spray-bottle"),
    # Reads the DERIVED flag, not `state.refreshState` itself. That field is an
    # object (`{"workReason":0,"workProcess":1}` in all 32 captured occurrences,
    # never a scalar), and a non-empty dict is truthy in the binary-sensor
    # template — so pointing at it directly latched the sensor ON the first time
    # the box ever deodorized and it could never read OFF again. It is also
    # presence-signalled rather than valued: it appears in only 3 of 905 state
    # reports, exactly the ones during a spray. See
    # `state_parsers._extract_presence_flags` for how absence becomes 0.
    EntityDef(component="binary_sensor", key="deodorization_running", name="Deodorization Running",
              value_path="state.deodorizing", device_class="running", icon="mdi:spray",
              entity_category="diagnostic"),
]
#: The T5 family's own hall switches, read live from a running T5 (firmware
#: 943) on 2026-07-31 under `sensor{}` — the same block name the W7H uses for a
#: completely different set. Diagnostic, because they say where the mechanism
#: physically is, which is what you want when a cycle stops half way and the
#: work mode alone cannot tell you where.
#:
#: No external source names these, so each entity carries the device's own
#: field name rather than an interpretation of it — `open_hall` becomes
#: "Hall: Open", not "Lid Open", which would be a guess about which part moves.
#: The box's `err{}` does corroborate that the firmware treats them as distinct
#: sensors: it carries a fault bit per hall (`hallT`, `hallD`, `hallS`, `hallO`,
#: `hallC`, `hallB`, `hallH`) alongside them.
#:
#: No `device_class`, for the same reason as the W7H's: nothing here pins the
#: polarity of each pin, and a class would render a wrong guess as a confident
#: answer instead of a raw 0/1 somebody can compare against the machine.
LITTER_CAMERA_HALL_SENSORS = [
    EntityDef(component="binary_sensor", key="hall_standby", name="Hall: Standby",
              value_path="state.stdby_hall", icon="mdi:electric-switch",
              entity_category="diagnostic"),
    EntityDef(component="binary_sensor", key="hall_smooth", name="Hall: Smooth",
              value_path="state.smooth_hall", icon="mdi:electric-switch",
              entity_category="diagnostic"),
    EntityDef(component="binary_sensor", key="hall_dump", name="Hall: Dump",
              value_path="state.dump_hall", icon="mdi:electric-switch",
              entity_category="diagnostic"),
    EntityDef(component="binary_sensor", key="hall_open", name="Hall: Open",
              value_path="state.open_hall", icon="mdi:electric-switch",
              entity_category="diagnostic"),
    EntityDef(component="binary_sensor", key="hall_close", name="Hall: Close",
              value_path="state.close_hall", icon="mdi:electric-switch",
              entity_category="diagnostic"),
    EntityDef(component="binary_sensor", key="hall_top", name="Hall: Top",
              value_path="state.top_hall", icon="mdi:electric-switch",
              entity_category="diagnostic"),
]

# REMOVED (2026-07-29): `purification_days` (state.purificationLeftDays). The
# string appears nowhere in real T5 firmware, while every other field on these
# lists does — it came from the reference integration, which models PetKit's
# CLOUD API, whose field names are not the device's.
#
# `garbage_bag_state` was removed alongside it and is BACK, below, for `t6`
# only. The reasoning was sound and the scope was not: it was checked against a
# T5, which has no bagging mechanism, and applied to every litter box. A T6
# sends `packageState` in all 3475 reports of a 67-hour capture.

#: The Purobot Ultra's waste-bagging mechanism. `t6` only — no other litter box
#: has the hardware, and publishing these where the field cannot arrive is what
#: got the first one deleted.
#:
#: Every one is raw. Observed values are -1/1 (`packState`, `baggingState`),
#: 0/1 (`sealDoorState`) and 0/2 (`boxStoreState`), and nothing names what any
#: of them mean — a label here would be invented, which is the failure mode
#: this whole file is arranged to avoid.
LITTER_PACKAGING_SENSORS = [
    EntityDef(component="sensor", key="garbage_bag_state", name="Bag State",
              value_path="state.packageState", icon="mdi:trash-can-outline",
              entity_category="diagnostic"),
    EntityDef(component="sensor", key="packing_state", name="Packing State",
              value_path="state.packState", icon="mdi:package-variant",
              entity_category="diagnostic"),
    EntityDef(component="sensor", key="bagging_state", name="Bagging State",
              value_path="state.baggingState", icon="mdi:package-variant-closed",
              entity_category="diagnostic"),
    EntityDef(component="sensor", key="seal_door_state", name="Seal Door State",
              value_path="state.sealDoorState", icon="mdi:door-sliding",
              entity_category="diagnostic"),
    EntityDef(component="sensor", key="box_store_state", name="Bin Store State",
              value_path="state.boxStoreState", icon="mdi:archive",
              entity_category="diagnostic"),
    EntityDef(component="sensor", key="package_count", name="Bags Left",
              value_path="state.packageCount", icon="mdi:counter"),
]

FEEDER_SENSORS = [
    EntityDef(component="sensor", key="device_status", name="Device Status",
              value_path="state.workingState", icon="mdi:state-machine"),
    EntityDef(component="sensor", key="error", name="Error",
              value_path="state.errorMsg", icon="mdi:alert-circle"),
    EntityDef(component="sensor", key="desiccant_days", name="Desiccant Days Left",
              value_path="state.desiccantLeftDays", unit="days", icon="mdi:water-outline"),
    EntityDef(component="sensor", key="rssi", name="WiFi Signal",
              value_path="state.rssi", device_class="signal_strength", unit="dBm"),
    # Both are running DAILY totals, summed from the feed events by
    # `events/normalize.py::_accumulate_feed_totals` -- no device reports them,
    # and on PetKit's own service the cloud does the same sum. `state_class`
    # matters because they reset at the device's midnight: without it HA's
    # long-term statistics read each rollover as the value genuinely falling.
    EntityDef(component="sensor", key="times_dispensed", name="Times Dispensed",
              value_path="state.feedState.times", icon="mdi:counter",
              state_class="total_increasing"),
    EntityDef(component="sensor", key="total_dispensed", name="Total Dispensed",
              value_path="state.feedState.realAmountTotal", unit="g", icon="mdi:scale",
              state_class="total_increasing"),
    EntityDef(component="sensor", key="food_in_bowl", name="Food in Bowl",
              value_path="state.weight", unit="g", icon="mdi:bowl"),
    EntityDef(component="sensor", key="food_bowl_pct", name="Food Bowl Level",
              value_path="state.bowl", unit="%", icon="mdi:bowl-mix"),
    EntityDef(component="sensor", key="amount_eaten", name="Amount Eaten",
              value_path="state.feedState.eatAmountTotal", unit="g", icon="mdi:food"),
    EntityDef(component="sensor", key="last_feed", name="Last Feed",
              value_path="state.lastFeed", device_class="timestamp", icon="mdi:food-fork-drink"),
]

FEEDER_BINARY_SENSORS = [
    # `state.foodLow` is DERIVED (`state_parsers._derive_food_low`), never the
    # raw `food`: `food` is presence -- a live D4H (fw 867) holds a steady 2
    # with a full hopper -- and discovery's generic truthy-is-ON template read
    # that as the problem being present, so a full hopper showed as Food Low.
    EntityDef(component="binary_sensor", key="food_low", name="Food Low",
              value_path="state.foodLow", device_class="problem", icon="mdi:food-drumstick-off"),
    EntityDef(component="binary_sensor", key="feeding", name="Feeding",
              value_path="state.feeding", device_class="running", icon="mdi:food"),
    EntityDef(component="binary_sensor", key="eating", name="Eating",
              value_path="state.eating", device_class="occupancy", icon="mdi:cat"),
    EntityDef(component="binary_sensor", key="battery_installed", name="Battery Installed",
              value_path="state.batteryPower", icon="mdi:battery"),
]

# --- D4H / D4SH (the embedded-Linux feeders) --------------------------------
# Everything here is a key in the state builder of a real D4SH 867 `ctrl` and is
# present in both of the real reports in issue #2. What each key MEANS is a
# separate question, and it is answered per entity below rather than in bulk:
# where nobody can say, the entity carries the device's own wording and no unit,
# because a sensor that reads confidently and lies is one you would act on.

#: The two hopper-level readings. DUAL-HOPPER ONLY, hence a D4SH-only list held
#: apart from FEEDER_NEXT_GEN_SENSORS the way FEEDER_DUAL_BUTTONS/NUMBERS are.
#:
#: They must be a separate list, not shared-and-excluded: `model_excludes` filters
#: only the family base, NOT `model_entities`, so excluding `hopper2_level` for a
#: D4H (as the code used to) never actually removed it. A single-hopper feeder
#: must simply not be GIVEN these — see `ha/categories.py`. A D4H reports the
#: singular `food` instead — CONFIRMED on a live D4H (fw 867): steady 2 with a
#: full hopper, 0 when nothing is detected.
#:
#: 2 = has food and 0 = empty, reported by the D4SH owner, who never saw 1 in
#: between. So this is not a percentage, and it is not a two-state either until
#: somebody sees the middle value — hence an enum rather than the binary "Food
#: Low" the family already has. An unmapped value renders as the raw number
#: (`_enum_sensor_value_template`), so a 1 that does turn up is visible.
FEEDER_DUAL_HOPPER_SENSORS = [
    EntityDef(component="sensor", key="hopper1_level", name="Hopper 1",
              value_path="state.food1", icon="mdi:silo",
              options=["Empty", "Has food"], option_values=[0, 2]),
    EntityDef(component="sensor", key="hopper2_level", name="Hopper 2",
              value_path="state.food2", icon="mdi:silo",
              options=["Empty", "Has food"], option_values=[0, 2]),
]

#: The single hopper-level reading for a one-hopper next-gen feeder (D4H). It is
#: the dual `hopper1_level` with the "1" dropped and pointed at the singular
#: `state.food` the rest of the feeder family uses.
#:
#: CONFIRMED on a live D4H (fw 867): it sends the singular `food` (like the
#: single-hopper cloud model), not `food1` (like the D4SH it shares a `ctrl`
#: with) — steady 2 with a full hopper, 0 when nothing is detected. Same 0/2
#: enum as the dual sensors above.
FEEDER_SINGLE_HOPPER_SENSORS = [
    EntityDef(component="sensor", key="hopper_level", name="Hopper",
              value_path="state.food", icon="mdi:silo",
              options=["Empty", "Has food"], option_values=[0, 2]),
]

FEEDER_NEXT_GEN_SENSORS = [
    # Leftover food, and NO unit on purpose. The family's `food_bowl_pct` reads
    # the same key as a percentage, which it is not: -1 is the firmware's "not
    # measured" (`recv feed start leftover set(-1)`, logged as a feed begins),
    # and the one real reading anybody has seen was 46 while surplus control was
    # being changed. Whether that 46 is grams, a percentage or a raw count is
    # exactly what nobody can currently say.
    EntityDef(component="sensor", key="bowl_surplus", name="Bowl Surplus",
              value_path="state.bowl", icon="mdi:bowl-mix",
              entity_category="diagnostic"),
    # Named for the field, because the field is all we have. The owner opened
    # the lid, pulled both hoppers and took the battery cover off, and this read
    # 1 throughout — so whatever it is, it is not the lid, and calling it "Lid"
    # would have been a confident lie about hardware.
    EntityDef(component="sensor", key="door_raw", name="door",
              value_path="state.door", icon="mdi:help-circle-outline",
              entity_category="diagnostic"),
    # Three infrared readings. Blocking a sensor did raise a "feed chute
    # blocked" fault, so at least one of them watches the chute — but none of
    # the three moved while the owner was doing it, so which is which is open.
    EntityDef(component="sensor", key="ir_b_1", name="ir_b_1",
              value_path="state.ir_b_1", icon="mdi:leak",
              entity_category="diagnostic"),
    EntityDef(component="sensor", key="ir_b_2", name="ir_b_2",
              value_path="state.ir_b_2", icon="mdi:leak",
              entity_category="diagnostic"),
    EntityDef(component="sensor", key="ir_c", name="ir_c",
              value_path="state.ir_c", icon="mdi:leak",
              entity_category="diagnostic"),
    # The DC line, in millivolts going by the 6228-6234 a mains-powered feeder
    # reports. `batV` is deliberately NOT published: it reads 0 with no backup
    # batteries fitted, which the owner confirmed is their case, and a battery
    # sensor pinned at 0 looks like a flat battery rather than an absent one.
    EntityDef(component="sensor", key="dc_millivolts", name="DC Voltage",
              value_path="state.DCV", unit="mV", device_class="voltage",
              entity_category="diagnostic"),
]

FEEDER_NEXT_GEN_HALL_SENSORS = [
    EntityDef(component="binary_sensor", key="hall_left", name="Hall: Left",
              value_path="state.left_hall", icon="mdi:electric-switch",
              entity_category="diagnostic"),
    EntityDef(component="binary_sensor", key="hall_home", name="Hall: Home",
              value_path="state.home_hall", icon="mdi:electric-switch",
              entity_category="diagnostic"),
    EntityDef(component="binary_sensor", key="hall_right", name="Hall: Right",
              value_path="state.right_hall", icon="mdi:electric-switch",
              entity_category="diagnostic"),
    EntityDef(component="binary_sensor", key="hall_left_sub", name="Hall: Left Sub",
              value_path="state.left_sub_hall", icon="mdi:electric-switch",
              entity_category="diagnostic"),
]

FOUNTAIN_SENSORS = [
    EntityDef(component="sensor", key="device_status", name="Device Status",
              value_path="state.workingState", icon="mdi:state-machine"),
    EntityDef(component="sensor", key="error", name="Error",
              value_path="state.errorMsg", icon="mdi:alert-circle"),
    EntityDef(component="sensor", key="rssi", name="WiFi Signal",
              value_path="state.rssi", device_class="signal_strength", unit="dBm"),
    EntityDef(component="sensor", key="filter_percent", name="Filter Level",
              value_path="state.filterPercent", unit="%", icon="mdi:filter"),
    EntityDef(component="sensor", key="filter_days", name="Filter Days Left",
              value_path="state.filterLeftDays", unit="days", icon="mdi:filter"),
    EntityDef(component="sensor", key="temperature", name="Water Temperature",
              value_path="state.heatRealTemp", device_class="temperature", unit="°C"),
    EntityDef(component="sensor", key="battery", name="Battery",
              value_path="state.batteryPercent", device_class="battery", unit="%"),
    EntityDef(component="sensor", key="drink_times", name="Drink Times",
              value_path="state.drinkTime", icon="mdi:cup-water"),
]

FOUNTAIN_BINARY_SENSORS = [
    EntityDef(component="binary_sensor", key="water_lack", name="Water Lack",
              value_path="state.lackWarning", device_class="problem", icon="mdi:water-off"),
    EntityDef(component="binary_sensor", key="low_battery", name="Low Battery",
              value_path="state.lowBattery", device_class="battery"),
    EntityDef(component="binary_sensor", key="replace_filter", name="Replace Filter",
              value_path="state.filterWarning", device_class="problem", icon="mdi:filter-remove"),
    EntityDef(component="binary_sensor", key="pet_detected", name="Pet Detected",
              value_path="state.detectStatus", device_class="occupancy", icon="mdi:cat"),
]

# --- W7H (EverSweet Ultra AI) ----------------------------------------------
# The W7H shares almost nothing with the fountains above: no filter, no
# battery, no `lackWarning`, and a mechanism the others do not have (a clean
# tank, a sewage tank, a lift valve, a heater and a disinfect cycle). Its state
# fields are the ones a real `property/post` carries, documented in the
# reverse-engineered map supplied 2026-07-31 and present key-for-key in that
# capture; `devices/state_parsers.py::W7H_STATE_FIELDS` is what produces them.
#
# `entity_category="diagnostic"` on most of them is deliberate: they are how you
# find out WHY the fountain stopped, not things to put on a dashboard. HA files
# them under Diagnostic so the primary card stays the three or four values
# somebody actually looks at.

FOUNTAIN_W7H_SENSORS = [
    # Timestamps, not counters. `drink_time` is when the pet last drank; the
    # map is explicit about it, and reading it as a count is how it nearly
    # ended up behind a "Drink Times" sensor showing 1785531049.
    EntityDef(component="sensor", key="last_drink", name="Last Drink",
              value_path="state.lastDrink", device_class="timestamp",
              icon="mdi:cup-water"),
    EntityDef(component="sensor", key="last_pet_detect", name="Last Pet Detected",
              value_path="state.lastPetDetect", device_class="timestamp",
              icon="mdi:cat"),
    # Integer state codes whose individual values are NOT known — published raw
    # rather than behind `options`, because a label per code would be invented.
    EntityDef(component="sensor", key="clean_tank_state", name="Clean Water Tank State",
              value_path="state.cwtState", icon="mdi:water", entity_category="diagnostic"),
    EntityDef(component="sensor", key="waste_tank_state", name="Waste Tank State",
              value_path="state.wtState", icon="mdi:delete-variant",
              entity_category="diagnostic"),
    # Why the device last restarted. A W7H that connects and then does nothing
    # is a real failure mode — one cost a support round-trip before a reboot
    # fixed it — and this is the field that says a restart happened at all.
    EntityDef(component="sensor", key="reboot_reason", name="Reboot Reason",
              value_path="state.rebootReason", icon="mdi:restart",
              entity_category="diagnostic"),
]

FOUNTAIN_W7H_BINARY_SENSORS = [
    # Consumable-equivalent: the one thing on this device that needs a human.
    #
    # `stgFullState` is the DRINKING TRAY, not the waste tank. A W7H owner said
    # so; the firmware settles it. In W7H 456 `ctrl` the field is written by
    # `set_prop(0x0d)`, whose value comes from the reader at 0x62774 — and that
    # reader's own name literal, used by its logger, is
    # `pk_hmi_get_water_tary_full_sta`. The log line printed one instruction
    # before the write reads "water tary full sta now=%d,read=%d".
    #
    # This shipped as "Waste Tank Full" from 1.1.0 on the opposite reading: that
    # a dedicated tray getter and a separate `taryF` error bit meant the tray
    # was already covered elsewhere, so `stg*` had to be the tank. The dedicated
    # getter is what FILLS this field. The whole `stg*` / `wt*` pair is the
    # other way round from what we assumed — see `waste_tank_installed` below.
    EntityDef(component="binary_sensor", key="tray_full", name="Tray Full",
              value_path="state.stgFullState", device_class="problem",
              icon="mdi:cup-water"),
    # Running states.
    EntityDef(component="binary_sensor", key="heating", name="Heating",
              value_path="state.heatState", device_class="running",
              icon="mdi:radiator"),
    EntityDef(component="binary_sensor", key="pump_running", name="Circulation Pump",
              value_path="state.pumpState", device_class="running",
              icon="mdi:pump"),
    # Two pumps, and the firmware names them: `pumpState` is read by the setter
    # logging "loop pump curr num", `waterPumpState` by the one logging "add
    # pump curr num". So this is the pump that DRAWS from the clean tank, not a
    # generic "transfer".
    EntityDef(component="binary_sensor", key="refill_pump_running", name="Refill Pump",
              value_path="state.waterPumpState", device_class="running",
              icon="mdi:pump"),
    EntityDef(component="binary_sensor", key="refilling", name="Refilling",
              value_path="state.addWaterState", device_class="running",
              icon="mdi:water-plus"),
    EntityDef(component="binary_sensor", key="flushing", name="Flushing",
              value_path="state.flushState", device_class="running",
              icon="mdi:water-sync"),
    EntityDef(component="binary_sensor", key="disinfecting", name="Disinfecting",
              value_path="state.disinfectState", device_class="running",
              icon="mdi:shimmer"),
    # Assembly, seated or not. All diagnostic: they answer "why won't it run".
    #
    # `stg*` is the TRAY and `wt*` is the WASTE tank — the opposite of what the
    # prefixes suggest, so do not "correct" them. Both writers were read out of
    # W7H 456 `ctrl`: `stgInstall` is given the return of the
    # predicate at 0x62766, the same one that guards the "Not work water tary
    # unstall" refusal, and `wtInstall` the return of 0x9d334, the predicate
    # behind "Not work dirty tank unstall".
    EntityDef(component="binary_sensor", key="tray_installed", name="Tray Installed",
              value_path="state.stgInstall", icon="mdi:cup-outline",
              entity_category="diagnostic"),
    EntityDef(component="binary_sensor", key="clean_tank_installed", name="Clean Water Tank Installed",
              value_path="state.cwtInstall", icon="mdi:water-check",
              entity_category="diagnostic"),
    EntityDef(component="binary_sensor", key="waste_tank_installed", name="Waste Tank Installed",
              value_path="state.wtInstall", icon="mdi:delete-empty",
              entity_category="diagnostic"),
    # NOT device_class "lock": HA reads that class as on = UNLOCKED, and this
    # field is 1 when the lock is CLOSED. The class would invert it silently.
    EntityDef(component="binary_sensor", key="waste_lock_closed", name="Waste Lock Closed",
              value_path="state.wtLock", icon="mdi:lock",
              entity_category="diagnostic"),
    EntityDef(component="binary_sensor", key="heater_installed", name="Heater Installed",
              value_path="state.heatInstall", icon="mdi:radiator-disabled",
              entity_category="diagnostic"),
    # Lift/valve mechanism.
    EntityDef(component="binary_sensor", key="lift_valve", name="Lift Valve",
              value_path="state.liftValveState", device_class="running",
              icon="mdi:valve", entity_category="diagnostic"),
    # The firmware calls this one the valve LOCATION, not motion: the reader
    # feeding `liftLiveState` logs "valve loacation sta now=%d,read=%d" (sic)
    # and names itself `pk_hmi_get_valve_location_sta`. Left as "Lift Moving"
    # because nobody has watched it change against the mechanism yet, and a
    # rename orphans the entity for the sake of a guess replacing a guess.
    EntityDef(component="binary_sensor", key="lift_moving", name="Lift Moving",
              value_path="state.liftLiveState", device_class="running",
              icon="mdi:arrow-up-down", entity_category="diagnostic"),
    EntityDef(component="binary_sensor", key="lift_resetting", name="Lift Resetting",
              value_path="state.liftResetState", device_class="running",
              icon="mdi:backup-restore", entity_category="diagnostic"),
    EntityDef(component="binary_sensor", key="refill_cooldown", name="Refill Cooldown",
              value_path="state.addWaterFrequent", icon="mdi:timer-sand",
              entity_category="diagnostic"),
    EntityDef(component="binary_sensor", key="device_power", name="Device Power",
              value_path="state.sw", device_class="power",
              entity_category="diagnostic"),
]

#: The raw hall switches behind the derived flags above, as diagnostics.
#:
#: Worth publishing separately because the derived flag can disagree with them
#: and the disagreement is the diagnosis: the real capture has `wtInstall`
#: reading 1 while `hall_DKR` reads 0, i.e. the waste tank is seated on one side
#: only. A user chasing "it says the tank is in but it won't flush" has no other
#: way to see that. (The pairing is `wtInstall` <- `hall_DKL`/`hall_DKR` and
#: `stgInstall` <- `hall_TY`, not the reverse — see the install block above.)
#:
#: Polarity is 1 = closed / magnet present / installed, as the firmware reports
#: it. The map warns that the exact NO/NC wiring may differ per pin, so these
#: carry no `device_class` — a wrong class would render an inverted pin as a
#: confident, wrong answer instead of a raw 0/1 somebody can compare against
#: what they just reseated.
FOUNTAIN_W7H_HALL_SENSORS = [
    EntityDef(component="binary_sensor", key="hall_clean_high", name="Hall: Clean Tank High",
              value_path="state.hall_CH", icon="mdi:electric-switch",
              entity_category="diagnostic"),
    EntityDef(component="binary_sensor", key="hall_clean_low", name="Hall: Clean Tank Low",
              value_path="state.hall_CL", icon="mdi:electric-switch",
              entity_category="diagnostic"),
    EntityDef(component="binary_sensor", key="hall_lock_left", name="Hall: Lock Left",
              value_path="state.hall_CKL", icon="mdi:electric-switch",
              entity_category="diagnostic"),
    EntityDef(component="binary_sensor", key="hall_lock_right", name="Hall: Lock Right",
              value_path="state.hall_CKR", icon="mdi:electric-switch",
              entity_category="diagnostic"),
    EntityDef(component="binary_sensor", key="hall_waste_full", name="Hall: Waste Tank Full",
              value_path="state.hall_DH", icon="mdi:electric-switch",
              entity_category="diagnostic"),
    EntityDef(component="binary_sensor", key="hall_waste_left", name="Hall: Waste Tank Left",
              value_path="state.hall_DKL", icon="mdi:electric-switch",
              entity_category="diagnostic"),
    EntityDef(component="binary_sensor", key="hall_waste_right", name="Hall: Waste Tank Right",
              value_path="state.hall_DKR", icon="mdi:electric-switch",
              entity_category="diagnostic"),
    EntityDef(component="binary_sensor", key="hall_lift_upper", name="Hall: Lift Upper",
              value_path="state.hall_LTU", icon="mdi:electric-switch",
              entity_category="diagnostic"),
    EntityDef(component="binary_sensor", key="hall_lift_lower", name="Hall: Lift Lower",
              value_path="state.hall_LTD", icon="mdi:electric-switch",
              entity_category="diagnostic"),
    EntityDef(component="binary_sensor", key="hall_tray", name="Hall: Drinking Tray",
              value_path="state.hall_TY", icon="mdi:electric-switch",
              entity_category="diagnostic"),
]

PURIFIER_SENSORS = [
    EntityDef(component="sensor", key="device_status", name="Device Status",
              value_path="state.workingState", icon="mdi:state-machine"),
    EntityDef(component="sensor", key="error", name="Error",
              value_path="state.errorMsg", icon="mdi:alert-circle"),
    EntityDef(component="sensor", key="humidity", name="Humidity",
              value_path="state.humidity", device_class="humidity", unit="%"),
    EntityDef(component="sensor", key="temperature", name="Temperature",
              value_path="state.temp", device_class="temperature", unit="°C"),
    EntityDef(component="sensor", key="air_purified", name="Air Purified",
              value_path="state.refresh", unit="m³", icon="mdi:air-purifier"),
    EntityDef(component="sensor", key="liquid", name="Liquid",
              value_path="state.liquid", unit="%", icon="mdi:water-percent"),
    EntityDef(component="sensor", key="battery", name="Battery",
              value_path="state.battery", device_class="battery", unit="%"),
]

PURIFIER_BINARY_SENSORS = [
    EntityDef(component="binary_sensor", key="spray", name="Spraying",
              value_path="state.refreshing", device_class="running", icon="mdi:spray"),
    EntityDef(component="binary_sensor", key="liquid_lack", name="Liquid Lack",
              value_path="state.liquidLack", device_class="problem", icon="mdi:water-off"),
]
