"""HA `event` entities for litter boxes and feeders — momentary occurrences.

A sensor answers "what is the state now"; these answer "something just
happened" (a pet visit, a cleaning cycle, a feed), which is what automations
actually trigger on. `events/normalize.py::entity_for_event` maps each device
event_type onto one of the entities below and fires it non-retained, so an HA
restart does not replay a visit that happened yesterday.

`options` is the list of event_types HA will accept: an event_type fired but
not listed is silently dropped by HA. It is therefore DERIVED from
`codes.MQTT_EVENT_TOPICS` — the same table the firing side dispatches on —
filtered by the entity's kind and the device family the list serves. A
hand-written copy is what used to be here, and it had drifted: a real T6's
`light_over` fired into a `cleaning_event` whose declared list stopped at the
cycles a T3 has. The device event_type strings themselves are not
capture-confirmed — see `events/normalize.py`'s header for what is.

`ha/categories.py` decides which of these lists a given device type gets.
"""
from petkit_local.events import codes
from petkit_local.ha.discovery import EntityDef

LITTER_EVENTS = [
    EntityDef(component="event", key="toilet_event", name="Toilet Event",
              icon="mdi:cat",
              options=codes.mqtt_event_names(codes.KIND_TOILET, codes.LITTER)),
    EntityDef(component="event", key="cleaning_event", name="Cleaning Event",
              icon="mdi:broom",
              options=codes.mqtt_event_names(codes.KIND_CLEANING, codes.LITTER)),
    EntityDef(component="event", key="error_event", name="Error Event",
              icon="mdi:alert",
              options=codes.mqtt_event_names(codes.KIND_ERROR, codes.LITTER)),
]

FEEDER_EVENTS = [
    EntityDef(component="event", key="feeding_event", name="Feeding Event",
              icon="mdi:food",
              options=codes.mqtt_event_names(codes.KIND_FEEDING, codes.FEEDER)),
]

#: The W7H's events. No other fountain publishes any: the Bluetooth EverSweets
#: run no jobs, report no drinking, and cannot reach us at all.
#:
#: The keys are fixed by `events/normalize.py::KIND_TO_ENTITY`, which dispatches on
#: the event's KIND — so this is `cleaning_event` even though what it fires is a
#: water-treatment job. `options` is narrowed to the fountain's own family: the
#: litter list (`work_continue`, `dump_over`, ...) names cycles this device does
#: not have. Before this existed a fountain's `work_start` published to a
#: discovery topic the fountain had never announced.
FOUNTAIN_W7H_EVENTS = [
    EntityDef(component="event", key="cleaning_event", name="Work Event",
              icon="mdi:water-sync",
              options=codes.mqtt_event_names(codes.KIND_CLEANING,
                                             codes.FOUNTAIN_NEXT_GEN)),
    EntityDef(component="event", key="drinking_event", name="Drinking Event",
              icon="mdi:cup-water",
              options=codes.mqtt_event_names(codes.KIND_DRINKING,
                                             codes.FOUNTAIN_NEXT_GEN)),
    EntityDef(component="event", key="error_event", name="Error Event",
              icon="mdi:alert",
              options=codes.mqtt_event_names(codes.KIND_ERROR,
                                             codes.FOUNTAIN_NEXT_GEN)),
]
