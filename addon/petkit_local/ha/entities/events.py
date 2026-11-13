"""HA `event` entities for litter boxes and feeders — momentary occurrences.

A sensor answers "what is the state now"; these answer "something just
happened" (a pet visit, a cleaning cycle, a feed), which is what automations
actually trigger on. `events/normalize.py::entity_for_event` maps each device
event_type onto one of the entities below and fires it non-retained, so an HA
restart does not replay a visit that happened yesterday.

`options` is the list of event_types HA will accept: an event_type fired but
not listed here is rejected by HA, so the two must be kept in step. The device
event_type strings themselves are not capture-confirmed — see
`events/normalize.py`'s header for what is.

`ha/categories.py` decides which of these lists a given device type gets.
"""
from petkit_local.ha.discovery import EntityDef

LITTER_EVENTS = [
    EntityDef(component="event", key="toilet_event", name="Toilet Event",
              icon="mdi:cat",
              options=["pet_in", "pet_out"]),
    EntityDef(component="event", key="cleaning_event", name="Cleaning Event",
              icon="mdi:broom",
              options=["work_start", "work_continue", "work_suspend",
                       "clean_over", "dump_over", "reset_over"]),
    EntityDef(component="event", key="error_event", name="Error Event",
              icon="mdi:alert",
              options=["error_start", "error_over"]),
]

FEEDER_EVENTS = [
    EntityDef(component="event", key="feeding_event", name="Feeding Event",
              icon="mdi:food",
              options=["feed_start", "feed_stop", "feed_over",
                       "eat_start", "eat_over"]),
]

#: The W7H's events. No other fountain publishes any: the Bluetooth EverSweets
#: run no jobs, report no drinking, and cannot reach us at all.
#:
#: The keys are fixed by `events/normalize.py::KIND_TO_ENTITY`, which dispatches on
#: the event's KIND — so this is `cleaning_event` even though what it fires is a
#: water-treatment job. `options` is not: HA silently rejects an event_type not
#: listed, and the litter list (`work_continue`, `dump_over`, ...) names cycles
#: this device does not have. Before this existed a fountain's `work_start`
#: published to a discovery topic the fountain had never announced.
FOUNTAIN_W7H_EVENTS = [
    EntityDef(component="event", key="cleaning_event", name="Work Event",
              icon="mdi:water-sync",
              options=["work_start", "add_water_over", "disinfect_over"]),
    EntityDef(component="event", key="drinking_event", name="Drinking Event",
              icon="mdi:cup-water",
              options=["drink_start", "drink_over"]),
    EntityDef(component="event", key="error_event", name="Error Event",
              icon="mdi:alert",
              options=["error_start", "error_over"]),
]
