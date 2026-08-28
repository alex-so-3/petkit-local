"""Device codename tables: which models exist, what they are and what they can do.

A device identifies itself only by a lowercase codename ("t5", "d4h", ...) — the
firmware never states its category, whether it has a camera or what its
commercial name is. Every such fact is answered from the sets below, so a new
model is supported by adding its codename here rather than by touching the
payload builders, entity tables and parsers that ask these questions.

The sets are the raw data; `devices/base.py` exposes them as the `Device.is_*`
predicates the rest of the code actually reads.
"""

#: The running version, shown in the panel's Setup tab and served by /api/info.
#:
#: A literal, not `importlib.metadata`: the Dockerfile copies the package in
#: rather than pip-installing it, so there is no distribution to ask, and the
#: add-on has no reliable way to read its own `config.yaml` at runtime.
#:
#: `tests/test_version.py` asserts it equals `config.yaml` and `pyproject.toml`,
#: which is what stops a bump from landing in two places out of three.
#:
#: The panel shows it because there is otherwise no way — for an owner or for
#: us — to tell which build is running: a stale entity set looks exactly like an
#: update that did not take.
VERSION = "2.1.0"

DEVICE_TYPES_LITTER = {"t3", "t4", "t5", "t6", "t7"}
DEVICE_TYPES_FEEDER = {"feeder", "feedermini", "d3", "d4", "d4s", "d4h", "d4sh"}

# Litter models that SEND `litter.usedTimes` and never fill it. The field is in
# every report the firmware emits and its value is a hard-coded zero, so passing
# it through publishes a Times Used that reads 0 while the cat is using the box —
# a wrong number, which is worse than an empty one.
#
# Evidence (T4, 2026-08-29): 209 stored state snapshots spanning 2026-08-16 to
# 2026-08-29 — every state block attached to every event this device reported,
# including the ones captured mid-visit and at `pet_out` — carry
# `litter.usedTimes: 0`, with no other value anywhere in the set. The same store
# holds 41 completed toilet visits over that window, so the box was in daily use
# throughout. The camera models are NOT in here: a real T5 report carries
# `usedTimes: 3` (tests/test_state_parsers.py), so theirs is a live counter.
#
# `t3` shares `_parse_litter_esp32` with the T4 and very likely behaves the same
# way, but no T3 capture exists to prove it and a guess here would suppress a
# field that might be real. Add it when someone confirms a T3 does the same.
#
# `events/normalize.py::_accumulate_visit_count` fills the gap by counting the
# device's own completed visits, the way the cloud does it.
DEVICE_TYPES_LITTER_USED_TIMES_UNREPORTED = {"t4"}

# Feeders with two hoppers, which dispense from each one separately.
#
# This is not cosmetic: the D4SH firmware (867, `ctrl`,
# `parse_service_invoke_msg`) compares its own model string against "D4SH" and
# in that branch reads ONLY `amount1`/`amount2` from a `feed_realtime`. The
# plain `amount` every other feeder takes is not looked at, so a dual-hopper
# sent one runs a feed cycle and dispenses nothing -- issue #2, where both
# hoppers reported `real_amount: 0` for a command that was well formed and
# asked for nothing.
#
# Membership is by codename because that IS the test the firmware makes: the
# string it compares is its own model, and `d4sh` is the codename for the
# YumShare Dual-Hopper. The neighbouring branch compares "D4H" and reads
# `amount`, which is why the single-hopper path below must stay exactly as it
# was -- see `ha/commands.py::_feed`.
DEVICE_TYPES_FEEDER_DUAL = {"d4sh"}

# Feeders running the embedded-Linux `ctrl` we have read (the D4SH 867 image
# serves both D4SH and D4H). Everything taken from that disassembly is gated on
# this set rather than applied to every feeder: the ESP32 models (D4, D3,
# Feeder Mini) run different firmware entirely, and extending a finding to them
# would be extrapolation, not evidence.
DEVICE_TYPES_FEEDER_NEXT_GEN = {"d4h", "d4sh"}
DEVICE_TYPES_WATER_FOUNTAIN = {"w4", "w5", "ctw2", "ctw3", "w7h"}
DEVICE_TYPES_PURIFIER = {"k2", "k3"}
DEVICE_TYPES_ALL = DEVICE_TYPES_LITTER | DEVICE_TYPES_FEEDER | DEVICE_TYPES_WATER_FOUNTAIN | DEVICE_TYPES_PURIFIER

# Models with no network of their own. They pair over BLE to a WiFi device --
# a litter box or a feeder -- which relays for them, so they never sign up, never
# hold MQTT credentials and never poll a heartbeat. Their readings arrive inside
# their parent's traffic (`devices/ble/`), and a `Device` for one of these
# codenames means something went wrong upstream, not that a new model appeared.
#
# The fountains were listed as network devices because PetKit's cloud API models
# them that way -- the account-side view is identical whether the fountain has
# WiFi or is being relayed, which is exactly why the distinction was missed.
#
# Evidence per model:
#   * w4, w5, ctw2 -- one BLE protocol, one pair of GATT UUIDs and one parser
#     serve all three in `phldgmn/ha-petkit-ble`, whose advertised names are
#     `Petkit_W5`/`W5C`/`W5N`, `Petkit_W4X`/`W4XUVC` and `Petkit_CTW2`. It is a
#     relay-less, cloud-less integration; a device with WiFi would not need it.
#   * ctw3 -- the CT-W3 manual: monitoring over Bluetooth, and remote access
#     only by pairing with a PetKit feeder or litter box within ~8 m acting as
#     the WiFi master. That master is the relay, not the fountain.
#   * k2, k3 -- the Pura Air spray, BLE-only since the first model.
#
# Only the W7H has WiFi of its own among the fountains.
DEVICE_TYPES_BLE_ONLY = {"w4", "w5", "ctw2", "ctw3", "k2", "k3"}

DEVICE_TYPES_CAMERA = {"t5", "t6", "t7", "d4h", "d4sh", "w7h"}
# Embedded-Linux models. Same membership as DEVICE_TYPES_CAMERA today, but a
# different question: "camera" decides media/STS behaviour, "next gen" decides
# how the device talks to us (HTTPS, MQTT-capable ctrl binary, BLE provisioning).
#
# The complement is not one thing. It holds the plain-HTTP WiFi models (T3/T4,
# the non-camera feeders — ESP32, going by the D4, Feeder Mini and T4 images)
# AND the models with no network at all (DEVICE_TYPES_BLE_ONLY). Reading it as
# "the ESP32 ones" is what put four Bluetooth fountains in the DNS-redirect
# instructions, where they can never belong.
DEVICE_TYPES_NEXT_GEN = {"t5", "t6", "t7", "d4h", "d4sh", "w7h"}

# Devices whose NPU runs on-device facial recognition (dev_discern_pic /
# dev_discern_config). This is only a SEED: `Device.supports_ai` also returns
# True once a device has actually asked for those endpoints, because a codename
# cannot answer the question on its own —
#
#   * PetKit's own "Pet Identification" screen lists three litter boxes (Purobot
#     Max Pro 2, Purobot Ultra, Purobot Max Pro), two feeders (YumShare Solo 2,
#     Dual-Hopper 2) and one fountain (EverSweet Ultra AI). `w7h` is here for
#     that last one.
#   * It also says the NON-"2" YumShare Solo and Dual-Hopper do NOT do
#     recognition — and both generations share one codename (`d4h` / `d4sh`;
#     localkit's own gen-2 branch handles the newer one as plain `d4sh` and
#     introduces no new name). So no list keyed on codename can be right for the
#     feeders, and they are deliberately absent: a gen-2 earns `supports_ai` by
#     polling, a gen-1 never does.
#   * "Purobot Max Pro" (without the 2) maps to no codename in this repo or in
#     localkit. Same resolution — it will identify itself by asking.
#   * `t7` is ours but is NOT on PetKit's list. Left in rather than removed:
#     nothing suggests it lacks the NPU, and dropping it would take the feature
#     away from anyone using one.
DEVICE_TYPES_AI = {"t5", "t6", "t7", "w7h"}

# Commercial product names per device codename. Verified against pypetkitapi
# doc/API.md, except T5/T6 which were corrected against a real device (T5 is a
# Purobot Max Pro 2). A codename that is not listed here is still supported —
# see `device_display_name` for what is shown for it.
#
# Two entries are known to be imprecise, and are left as they are because the
# alternative is a guess:
#   * `d4h`/`d4sh` — PetKit sells a "2" of each (YumShare Solo 2, Dual-Hopper 2)
#     that shares the codename, so one name here covers two products that differ
#     in whether they can recognise a pet at all. See DEVICE_TYPES_AI above.
#   * `t7` — localkit calls it "Purobot Crystal"; pypetkitapi says "Crystal Duo".
#     Unresolved without hardware.
#   * `k2` — the generation before the K3, named by analogy. PetKit's own listing
#     for it could not be found.
#
# `d4` was "Feeder D4" — the codename dressed up as a product, which is what
# this table exists to avoid. Its owner named it in issue #3: a **Fresh Element
# Solo**. `d3` and `d4s` are still the codename echoed back, for want of anybody
# to ask; if you own one, the name on the box is the whole contribution needed.
#
# `k2`/`k3` were "Air Purifier K2"/"Air Purifier K3" here, which is wrong and
# actively misleading: PetKit also sells real air purifiers. These are the Pura
# Air smart spray, a Bluetooth deodoriser that mounts inside a litter box (or on
# a wall) and sprays after a visit. Several retailers do list the K3 as an "air
# purifier", which is presumably where the name came from.
DEVICE_NAMES = {
    "feeder": "Feeder",
    "feedermini": "Feeder Mini",
    "d3": "Feeder D3",
    "d4": "Fresh Element Solo",
    "d4s": "Feeder D4s",
    "d4h": "YumShare Solo",
    "d4sh": "YumShare Dual-Hopper",
    "t3": "Pura X",
    "t4": "Pura Max",
    "t5": "Purobot Max Pro 2",
    "t6": "Purobot Ultra",
    "t7": "Purobot Crystal Duo",
    "w4": "EverSweet",
    "w5": "EverSweet 3 Pro",
    "ctw2": "EverSweet Solo 2",
    "ctw3": "EverSweet Max Cordless",
    "w7h": "EverSweet Ultra AI",
    "k2": "Pura Air",
    "k3": "Pura Air K3",
}

# The object-key prefix our own `dev_upload_log_token` hands out as `pathPrefix`,
# and the one key shape `http/bucket.py` stores OUTSIDE the media tree. It lives
# here because the two ends must agree exactly and they sit in different layers:
# `devices/payloads.py::to_log_upload_token` mints it, `http/bucket.py::_route`
# consumes it. It must never collide with a device codename, because a media key
# begins with one (`{device_type}/{petkit_id}/{capability}`) — asserted in tests.
DEVICE_LOG_KEY_PREFIX = "devlog"


def device_display_name(code: str) -> str:
    """The commercial product name for a device codename.

    An unlisted codename yields "Unknown" rather than the code itself: such a
    device is still fully supported (it registers, connects and publishes
    entities), and the panel shows the raw codename next to this name anyway, so
    echoing the code here would only duplicate it. An empty code yields an empty
    string, because there is nothing to name.
    """
    if not code:
        return ""
    return DEVICE_NAMES.get(code.lower(), "Unknown")
