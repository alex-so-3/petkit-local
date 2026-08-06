"""The `Device` model: one registered PetKit device and the payloads it is served.

A device never sees our data structures — it sees JSON bodies on a handful of
device-facing HTTP endpoints, and the firmware is unforgiving about their exact
shape (see `to_signup` and `to_oss_sts`, whose comments record what was learned
the hard way). Every one of those bodies is generated here, next to the state it
is built from, so a field's type cannot drift between the HTTP handler that
serves it and the MQTT service reply that mirrors it.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from petkit_local.utils.const import (
    DEVICE_LOG_KEY_PREFIX,
    DEVICE_TYPES_AI,
    DEVICE_TYPES_BLE_ONLY,
    DEVICE_TYPES_CAMERA,
    DEVICE_TYPES_FEEDER,
    DEVICE_TYPES_LITTER,
    DEVICE_TYPES_NEXT_GEN,
    DEVICE_TYPES_PURIFIER,
    DEVICE_TYPES_WATER_FOUNTAIN,
)
from petkit_local.devices.state_parsers import CONSUMABLE_RECORD_KEY, SPRAY_TOTAL_DAYS
from petkit_local.utils.coerce import to_bool, to_float
from petkit_local.utils.crypto import generate_device_secret, generate_product_key
from petkit_local.utils.timeutil import local_offset_hours


class Refused(ValueError):
    """The write was understood and rejected, and nothing was changed.

    Distinct from a `None` return, which means "applied, but there is nothing to
    send to the device" -- a capability toggle, a schedule write. Both used to be
    `None`, so the panel answered `{"ok": true}` to a value it had just thrown
    away, which is the silent failure that refusing was supposed to avoid.

    Lives here rather than in `ha/commands.py` because a BLE accessory refuses
    writes too (`devices/ble.py`), and `devices/` cannot import `ha/`. Two
    classes of one name reaching the same `except` in `web/panel.py` is the
    trap this avoids.
    """


if TYPE_CHECKING:  # `devices.ble` imports `devices.registry`, which imports us.
    from petkit_local.devices.ble import BLERegistry

log = logging.getLogger(__name__)


def split_bucket_authority(bucket_endpoint: str) -> tuple[str, str] | None:
    """Split our bucket address into the `(bucketName, endPoint)` pair the
    device's `logUpload` concatenates back with a literal dot.

    `dev_upload_log_token` gives the firmware no way to express a plain host:
    the upload URL comes from `https://%s.%s%s/%s`, so the authority is always
    `{bucketName}.{endPoint}`. To be reachable at all, our own address has to be
    cut at a dot and handed over in two pieces:

        https://192.0.2.199:9000  ->  ("192", "0.2.199:9000")

    The cut is at the FIRST dot rather than the last, which matters for one
    reason: the device rebuilds the same string either way, but a real OSS
    bucket name may not contain a dot, so `192` is a plausible `bucketName`
    where `192.0.2` is not. If a firmware ever sanity-checks that field, this
    is the form that survives.

    The port stays on `endPoint`, which is safe here and would NOT be in
    `to_oss_sts`: `primaryDomain` there goes through
    `sscanf("https://%[^/]/%s")` into `getaddrinfo`, which a port breaks. This
    value is only ever sprintf'd into a URL and handed to curl, which parses a
    port correctly. Should that turn out to be wrong on hardware, moving the
    bucket to 443 and dropping the suffix is a change to this function alone.

    Returns:
        The two pieces, or None when no valid authority can be formed — an
        empty address (there is no add-on option for `bucket_endpoint`, so an
        install where the Supervisor host-IP lookup failed has none), a
        single-label host like `localhost:9000`, an IPv6 literal, or anything
        carrying userinfo. The caller answers `{"result": {}}` for all of them.
    """
    if not bucket_endpoint:
        return None
    authority = bucket_endpoint.split("//", 1)[-1].split("/", 1)[0]
    # `@` and `[` would each need a URL-building rule of their own, and neither
    # can appear in an address this add-on generates for itself.
    if not authority or "@" in authority or "[" in authority:
        return None
    head, dot, tail = authority.partition(".")
    if not dot or not head or not tail:
        return None
    return head, tail


@dataclass
class Device:
    """One registered PetKit device: its identity, credentials and live state.

    Created on `dev_signup` and persisted to `devices.json` by `DeviceRegistry`.
    Only identity, credentials and `config` survive a restart (`to_dict`);
    `state`, `command_queue` and the liveness timestamps are rebuilt from the
    device's next contact, so nothing transient may become the only copy of
    something we cannot re-derive.

    The MQTT triple (`mqtt_product_key`, `mqtt_device_name`,
    `mqtt_device_secret`) is the exception: it is minted here exactly once and
    handed to the device by `to_iot_device_info*`. Losing it leaves the device
    connecting with credentials the broker no longer knows, which is why
    `DeviceRegistry` writes a new device through synchronously.

    The `to_*` methods generate the response bodies for the device-facing
    endpoints, `{"result": ...}` wrapper included, so a handler stays a
    one-liner and the firmware-mandated shape lives next to the fields it is
    built from instead of being spread across `http/handlers/`. They are
    intended to be pure, and all but one are: `to_device_info` shares (rather
    than copies) `config["settings"]` into its result and can write a `k3Config`
    key back through it — see its own docstring.
    """

    device_type: str
    petkit_id: int
    serial_number: str = ""
    mac: str = ""
    firmware: str = ""

    mqtt_product_key: str = field(default_factory=generate_product_key)
    mqtt_device_name: str = ""
    mqtt_device_secret: str = field(default_factory=generate_device_secret)

    #: The credential the device signs its HTTP requests with, handed to it by
    #: `to_signup`. Empty means "use `mqtt_device_secret`", which is what every
    #: device had before proxy mode could learn a real one — see
    #: `signing_secret`.
    api_secret: str = ""

    state: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    # Entries are either a command dict or an already-JSON-encoded string
    # (`patchers/common.py` queues the encoded form); the heartbeat handler
    # passes both through unchanged.
    command_queue: list[Any] = field(default_factory=list)

    last_heartbeat: float = 0.0
    last_state_report: float = 0.0
    last_seen: float = 0.0  # any HTTP contact from the device
    #: Last frame received from the device over MQTT. A device that gets onto
    #: the broker STOPS polling `poll/{type}/heartbeat` — confirmed on a T5,
    #: which went quiet over HTTP some 40s after its CONNECT — so without this
    #: the liveness check sees only silence and calls a perfectly healthy device
    #: offline (`ha/publisher.py::device_is_stale`).
    last_mqtt: float = 0.0
    #: Topic filters in force for this device's current MQTT session — the ones
    #: it asked for, plus the ones subscribed on its behalf
    #: (`mqtt/auth.py::_server_subscribe`, which is the only source for a T5:
    #: it sends no SUBSCRIBE at all). Live state, reset per session by
    #: `_mark_mqtt_connected`. Exists because a publish to an unsubscribed
    #: topic succeeds silently, so this is the only way to tell a command that
    #: was delivered from one that went nowhere.
    mqtt_subscriptions: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    online: bool = False
    mqtt_connected: bool = False
    #: When `mqtt_connected` last went up. The heartbeat's `iotStatus` is the
    #: backstop that takes it down again, and the device samples that value
    #: before it sends the request — so a poll already in flight when the
    #: session came up arrives reporting the state from just before it. Without
    #: this timestamp that stale zero cancels the connect that beat it by
    #: milliseconds; `http/handlers/heartbeat.py::note_iot_status` uses it to
    #: tell a lagging report from a real loss.
    mqtt_connected_at: float = 0.0

    def __post_init__(self) -> None:
        """Derive the MQTT device name when the caller did not supply one."""
        if not self.mqtt_device_name:
            sn = self.serial_number or str(self.petkit_id)
            self.mqtt_device_name = f"d_{self.device_type}_{sn}"

    # The category predicates below are plain reads of the type sets in
    # `utils/const.py`; they are properties because the payload builders in this
    # class branch on them constantly and a set lookup reads badly inline.

    @property
    def is_camera(self) -> bool:
        """Whether the model has a camera, and so media uploads and STS capabilities."""
        return self.device_type in DEVICE_TYPES_CAMERA

    @property
    def is_next_gen(self) -> bool:
        """Whether this is an embedded-Linux model rather than an ESP32 one."""
        return self.device_type in DEVICE_TYPES_NEXT_GEN

    @property
    def is_litter(self) -> bool:
        """Whether this is a litter box, which selects the litter settings block."""
        return self.device_type in DEVICE_TYPES_LITTER

    @property
    def is_feeder(self) -> bool:
        """Whether this is a feeder, which selects the feeder settings block."""
        return self.device_type in DEVICE_TYPES_FEEDER

    @property
    def is_water_fountain(self) -> bool:
        """Whether this is a fountain, which selects the fountain settings block."""
        return self.device_type in DEVICE_TYPES_WATER_FOUNTAIN

    @property
    def is_purifier(self) -> bool:
        """Whether this is an air purifier (K2/K3, BLE-only in practice)."""
        return self.device_type in DEVICE_TYPES_PURIFIER

    @property
    def is_ble_only(self) -> bool:
        """Whether this model has no network of its own.

        True means this `Device` should not exist: the real thing pairs over
        BLE to a WiFi device that relays for it (`devices/ble.py`), so it can
        neither sign up nor hold credentials nor poll. Read by
        `registry.get_or_create`, which warns rather than refuses -- a device
        is never told no. Nothing else branches on it: one that cannot arrive
        needs no special handling once it somehow has.
        """
        return self.device_type in DEVICE_TYPES_BLE_ONLY

    @property
    def supports_ai(self) -> bool:
        """Whether the device's own NPU runs facial recognition (`dev_discern_pic`).

        The codename set is only a seed. A device that does recognition asks for
        `dev_discern_config` at every boot and roughly hourly after that, and one
        that cannot never asks at all — so the device answers this question
        better than any list of product names can. (The reference capture shows
        122 of these in three hours, but that was a device boot-looping on a
        cloud error: 117 of the gaps are under ten seconds and the count tracks
        `dev_signup` almost exactly. It is a per-boot poll, not a fast one.)

        That matters most for the feeders, where PetKit ships two generations
        under one codename and only the newer has an NPU — see
        `utils/const.py::DEVICE_TYPES_AI`.

        `ai_observed` is set by `http/handlers/discern.py` and only ever turns
        ON: a device being offline, asleep, or simply not having polled yet is
        not evidence that it lacks the hardware.
        """
        return (self.device_type in DEVICE_TYPES_AI
                or to_bool(self.config.get("ai_observed"), False))

    # Media capabilities are the STS `capability[]` entries (see to_oss_sts) —
    # the upload types the device is allowed to ask for. Toggling one off
    # drops it from the next STS response, so the device stops uploading that
    # type at the source (no bandwidth/disk wasted on a discarded upload).
    CAPABILITY_TYPES = ("fullVideo", "eventImage", "highLight", "dynamicVideo")

    def enabled_capabilities(self) -> set[str]:
        """Which of `CAPABILITY_TYPES` this device may currently upload.

        Empty for a camera-less model, which has no media pipeline at all. A
        camera model with nothing stored under `config["capabilities"]` has
        never been configured, and defaults to all types on.
        """
        if not self.is_camera:
            return set()
        stored = self.config.get("capabilities")
        if stored is None:
            return set(self.CAPABILITY_TYPES)  # default: all on
        return {ct for ct in self.CAPABILITY_TYPES if stored.get(ct, True)}

    def to_signup(self) -> dict[str, Any]:
        """`dev_signup` — the identity and secret the device keeps from now on.

        The firmware is STRICT about two fields: `signupAt` must be a STRING and
        `createdAt` a NUMBER, and both must be present, or the device never
        advances past signup (it re-loops the boot forever).

        CONFLICT, deliberately unresolved: the real cloud sends NEITHER field
        (captured 2026-07-27, 135 replies). We keep them anyway. The boot-loop
        above was observed on hardware at signup time, whereas the capture comes
        from a device that was already registered — so the capture cannot show
        what a first-time signup needs. Extra keys cost nothing; dropping a
        documented boot-loop guard on this evidence would be a bet with the
        device's usability as the stake.

        The remaining fields are additive, straight from that capture.
        """
        ts = int(self.created_at)
        return {
            "result": {
                "id": self.petkit_id,
                "signupAt": str(ts),
                "createdAt": ts,
                "secret": self.signing_secret,
                "mac": self.mac,
                "sn": self.serial_number,
                "timezone": self.timezone_offset,
                "locale": self.config.get("locale", ""),
                "shareOpen": 0,
                "petInTipLimit": 0,
                "p2pType": 2,
                "tooManyPets": 0,
                "frequencyPetTip": 0,
                "deodorantTip": 0,
                "purificationTip": 0,
            }
        }

    @property
    def signing_secret(self) -> str:
        """What `secret` to hand the device — the real one if we have learned it.

        The firmware signs every request with this: the `X-Device` header carries
        `id`, `nonce`, `timestamp`, `type` and an MD5 `sign` computed from them
        and this secret. We never verify that signature, so locally any value
        works — but PetKit does verify it, and answers `{"error": {"code": 704}}`
        to a signature made with a secret it does not know, which is every
        signature made with one WE generated.

        Proxy mode's whole point is seeing real cloud replies, so when a proxied
        `dev_signup` reveals the device's genuine secret it is adopted here
        (`http/middleware.py::_remember_upstream_credentials`) and handed on
        unchanged. The device then signs with a credential PetKit accepts, and
        we never had to reproduce the signature algorithm — the device computes
        it. Note this is NOT `mqtt_device_secret`: the real cloud issues two
        different values, 16 hex here and 32 for the broker.

        Falls back to `mqtt_device_secret` so a device that has never been
        proxied behaves exactly as before.
        """
        return self.api_secret or self.mqtt_device_secret

    @property
    def timezone_offset(self) -> float:
        """Hours east of UTC to report to the device, e.g. 2.0 for CEST.

        Derived from the container's timezone, which the Supervisor sets from
        Home Assistant's own configuration, and overridable per device via
        `config["timezone"]` for an install whose box lives in another zone.

        Three sources, most specific first: the manual override, then whatever
        the device itself reported at signup, then the container's clock. The
        middle one matters because the device is the authority on where IT is —
        it was handed that offset over BLE at provisioning and burns it into its
        video watermarks — so answering with the server's instead told a device
        in UTC-4 that it was at UTC+2.

        This used to be hardcoded to 1.0, which is wrong for half the year
        anywhere that observes DST. Note that fixing it does NOT fix the
        device's video watermarks: the firmware takes its timezone from the
        BLE provisioning payload, not from this response, so a device
        provisioned without one renders UTC regardless of what we say here.
        """
        override = to_float(self.config.get("timezone"), None)
        if override is not None:
            return override
        reported = to_float(self.config.get("reported_timezone"), None)
        if reported is not None:
            return reported
        return local_offset_hours()

    @property
    def aliyun_mqtt_host(self) -> str:
        """The Aliyun-format MQTT hostname the firmware expects.

        It does NOT resolve on the LAN, so it is only ever a safe fallback for
        when we cannot derive our own broker host: the device's MQTT connect
        then DNS-fails and it settles on the HTTP heartbeat, which is a working
        degraded mode rather than a failure.
        """
        return f"{self.mqtt_product_key}.iot-as-mqtt.eu-central-1.aliyuncs.com"

    def resolve_mqtt_host(self, real_host: str) -> str:
        """Which `mqttHost` to hand this device.

        Always our own broker (`real_host`): a patched `ctrl` connects to it
        over MQTT, and an unpatched one simply keeps heartbeating over HTTP — it
        does NOT crash, confirmed on-device. `aliyun_mqtt_host` is used only
        when we could not determine our own address at all.
        """
        return real_host or self.aliyun_mqtt_host

    def to_iot_device_info(self, mqtt_host: str) -> dict[str, Any]:
        """`dev_only_iot_device_info[_v2]` — Ingenic/next-gen path, ali-wrapped."""
        return {
            "result": {
                "ali": {
                    "id": self.petkit_id,
                    "deviceName": self.mqtt_device_name,
                    "deviceSecret": self.mqtt_device_secret,
                    "iotPlatform": "ALI",
                    "iotInstanceId": self.mqtt_product_key,
                    "productKey": self.mqtt_product_key,
                    "mqttHost": mqtt_host or self.aliyun_mqtt_host,
                    "createdAt": int(self.created_at * 1000),
                    "type": 1,
                    "regionId": "eu-central-1",
                }
            }
        }

    def to_iot_device_info_flat(self, mqtt_host: str) -> dict[str, Any]:
        """`dev_iot_device_info` — ESP32 path, FLAT block (no `ali` wrapper).

        A different endpoint with a different schema from
        `to_iot_device_info` (localkit's DevIotDeviceInfoResource): returning
        the `ali`-wrapped block here leaves ESP32 devices unable to read their
        MQTT credentials at all.
        """
        return {
            "result": {
                "id": self.petkit_id,
                "deviceName": self.mqtt_device_name,
                "deviceSecret": self.mqtt_device_secret,
                "iotPlatform": "ALI",
                "iotInstanceId": self.mqtt_product_key,
                "productKey": self.mqtt_product_key,
                "mqttHost": mqtt_host or self.aliyun_mqtt_host,
                "createdAt": int(self.created_at * 1000),
                "type": 1,
                "regionId": "eu-central-1",
            }
        }

    def to_serverinfo(self, api_url: str) -> dict[str, Any]:
        """`dev_serverinfo` — where the device should send everything from now on.

        `nextTick` is how long the device waits before asking again, and 3600 is
        the real cloud's answer (captured 2026-07-27). The 30 this used to send
        cost ~2800 needless requests a day; the device's own heartbeat is what
        detects a lost connection, not this poll.

        `dns` stays an empty STRING and `ipServers` an empty ARRAY — both proven
        against a real T5. The cloud sends a six-element `dns` list and one
        `ipServers` entry, but a populated list here would be an untested shape
        on a firmware that NULL-derefs empty arrays elsewhere
        (`dev_ble_device`), and neither field buys us anything: we are the only
        server, reachable by address.
        """
        return {
            "result": {
                "apiServers": [api_url],
                "ipServers": [],
                "dns": "",
                # The cloud includes this, always null. Cheap to match.
                "pimServers": None,
                "linked": 1,
                "nextTick": 3600,
            }
        }

    def to_device_info(self, ble_registry: BLERegistry | None = None) -> dict[str, Any]:
        """`dev_device_info` — the device's own full configuration, as it sees it.

        Camera litters additionally get the `capacity[]` / `cloudProduct` block
        that stands in for a cloud subscription, and, when `ble_registry` is
        given, the embedded K3 purifier block (`withK3`, `k3Device`,
        `settings.k3Config`) for whichever K3 is linked to this device.

        NOTE, and unlike its siblings, this method is NOT pure: the `settings`
        block in the result is the device's own `config["settings"]` dict rather
        than a copy, so writing `k3Config` into it also writes it into the
        stored config that gets persisted, where it outlives the K3 being
        unlinked (nothing removes the key again).
        """
        settings = self.config.get("settings", {})
        result = {
            "id": self.petkit_id,
            "mac": self.mac,
            "sn": self.serial_number,
            "secret": self.signing_secret,
            "timezone": self.timezone_offset,
            "locale": self.config.get("locale", ""),
            "shareOpen": 0,
            "modelCode": 2,
            "btMac": self.config.get("bt_mac", ""),
            "settings": settings if settings else self.default_settings(),
            "multiConfig": True,
            "petInTipLimit": 15,
            "p2pType": 2,
            "serviceStatus": 1,
            "hertz": 50,
        }

        if self.is_litter and self.is_camera:
            now = int(time.time())
            far = 4102444800
            result["sprayDays"] = SPRAY_TOTAL_DAYS
            # Falls back to the stamp we recorded, because `state` is empty for
            # the first moments after a restart and the firmware has a setter
            # for this field (`set sprayResetTime (%d)` in `ctrl`). Echoing a
            # zero there would push the N60 countdown's origin back to now on
            # the box itself, silently costing the owner the rest of a
            # cartridge's warning. PetKit's own reply carries the true value.
            recorded = (self.config.get(CONSUMABLE_RECORD_KEY) or {}).get("n60")
            result["sprayResetTime"] = (to_float(self.state.get("sprayResetTime"), 0)
                                        or to_float(recorded, 0))
            result["tooManyPets"] = 0
            result["frequencyPetTip"] = 0
            result["deodorantTip"] = 0
            result["purificationTip"] = 0
            # Mirrors to_oss_sts's capability[] set — a disabled capability
            # must disappear from BOTH so the device doesn't see conflicting
            # answers about what it's allowed to upload.
            result["capacity"] = [
                {"name": ct, "workTime": now, "indate": far}
                for ct in self.CAPABILITY_TYPES if ct in self.enabled_capabilities()
            ]
            result["cloudProduct"] = {
                "serviceId": 0,
                "name": "Local",
                "workTime": now,
                "workIndate": far,
                "chargeType": "LOCAL",
                "subscribe": 0,
            }

        if ble_registry and self.is_litter:
            k3 = ble_registry.get_linked_k3(self.petkit_id)
            if k3:
                result["withK3"] = 1
                result["k3Id"] = k3.petkit_id
                result["k3Device"] = {
                    "id": k3.petkit_id,
                    "mac": k3.mac,
                    "sn": k3.serial_number,
                    "secret": k3.secret,
                }
                result["settings"]["k3Config"] = {"config": k3.config}
            else:
                result["withK3"] = 0

        return {"result": result}

    def default_settings(self) -> dict[str, Any]:
        """The settings block a device of this category starts life with.

        Seeded into `config["settings"]` at registration and served by
        `to_device_info` until the device or Home Assistant overwrites a key, so
        every switch/number/select entity has a value to render on day one.
        Missing keys are also backfilled on load, see
        `devices/registry.py::_merge_default_settings`. Empty for a codename no
        category claims.

        The litter set is checked against a captured `dev_device_info` from the
        real cloud (2026-07-27). `lightRange`/`disturbRage` are NOT in it — the
        cloud carries those ranges in `dev_multi_config` instead — so they are
        no longer seeded. A device that already has them keeps them; nothing
        strips stored settings, because those are the owner's state.
        """
        if self.is_litter:
            base = {
                "manualLock": 0, "clickOkEnable": 1,
                "avoidRepeat": 1, "underweight": 1, "kitten": 0,
                "bury": 0, "sandType": 0, "autoWork": 1,
                "fixedTimeClear": 0, "autoIntervalMin": 0,
                "stillTime": 30, "stopTime": 600, "unit": 0,
                "language": "en_US", "deepClean": 0, "disturbMode": 0,
                "lightest": 1680, "downpos": 0, "sandSaving": 0,
                "lightMode": 0, "lightConfig": 1,
                "lightMultiRange": [],
            }
            if self.is_camera:
                base.update({
                    "camera": 1, "microphone": 1, "night": 1,
                    "timeDisplay": 1, "tumbling": 0,
                    "cameraLight": 1, "highlight": 1,
                    "autoProduct": 0, "upload": 1,
                    "preLive": 1, "liveEncrypt": 1,
                    "toiletDetection": 1, "petDetection": 1,
                    "petNotify": 1, "petNotifyInterval": 60,
                    "lightAssist": 1, "toiletLight": 0,
                    "toneMode": 0, "toneMultiRange": [[1320, 360]], "toneConfig": 2,
                    "systemSoundEnable": 1, "volume": 1,
                    "deepSpray": 0, "fixedTimeSpray": 1, "autoSpray": 1,
                    "autoIntervalSpray": 0,
                    "sandFullWeight": [3500, 5800, 3000, 3500, 3500],
                    "sandSetUseConfig": [[2, 2, 4]] * 4,
                    "deodorantNotify": 1, "sprayNotify": 1,
                    "phDetection": 0, "voice": 1, "logSwitch": 1,
                })
            return base
        if self.is_feeder:
            base = {
                "manualLock": 0, "lightMode": 0, "foodWarn": 0,
                "factor": 10,
            }
            if self.is_camera:
                base.update({
                    "camera": 1, "microphone": 1, "night": 1,
                    "timeDisplay": 1, "moveDetection": 1, "moveSensitivity": 1,
                    "petDetection": 1, "petSensitivity": 3,
                    "eatDetection": 1, "eatSensitivity": 3,
                    "soundEnable": 0, "systemSoundEnable": 0,
                    "volume": 4, "smartFrame": 1,
                })
            return base
        if self.is_water_fountain:
            return {
                "manualLock": 0, "lightMode": 0, "disturbMode": 0,
                "addWaterSwitch": 0, "petDetection": 0, "heaterSwitch": 0,
                "fountainMode": 0, "fountainTime": 12, "sleepTime": 12,
            }
        if self.is_purifier:
            return {
                "lightMode": 0, "manualLock": 0, "sound": 0,
            }
        return {}

    def to_multi_config(self) -> dict[str, Any]:
        """`dev_multi_config` — the schedule ranges (light, do-not-disturb, camera).

        Every value in the result is a JSON-encoded STRING, not a nested object
        (verified against the real PetKit cloud), and each such string wraps its
        own key again. `cameraMultiRange` is an array of schedule objects
        `[{enable, rpt, time}]` rather than a bare range pair.

        The `distrubMultiRange` misspelling is intentional: it is what the
        firmware sends and expects, so correcting it here would silently drop
        the do-not-disturb schedule.
        """
        def mc(key: str, val: Any) -> str:
            """Wrap one range in its own key and encode it as a compact string."""
            return json.dumps({key: val}, separators=(",", ":"))

        if self.is_litter:
            result = {
                "lightMultiRange": mc("lightMultiRange", [[0, 1440]]),
                "distrubMultiRange": mc("distrubMultiRange", [[40, 520]]),
            }
            if self.is_camera:
                result["cameraMultiRange"] = mc("cameraMultiRange", [
                    {"enable": 1, "rpt": "1,2,3,4,5,6,7", "time": [[0, 1440]]}
                ])
                result["toneMultiRange"] = mc("toneMultiRange", [[1320, 360]])
            return {"result": result}
        if self.is_feeder and self.is_camera:
            return {"result": {
                "detectMultiRange": mc("detectMultiRange", [[0, 1440]]),
                "cameraMultiNew": mc("cameraMultiNew", [[0, 1440]]),
                "toneMultiRange": mc("toneMultiRange", [[1320, 360]]),
                "lightMultiRange": mc("lightMultiRange", [[0, 1440]]),
            }}
        return {"result": {}}

    def to_video_device_info(self) -> dict[str, Any]:
        """`dev_video_device_info` — empty Agora credentials.

        Live view runs over the local RTSP/go2rtc path, so the device must be
        told there is no Agora account rather than being left to retry one.
        """
        return {
            "result": {
                "agora": {"license": "", "appId": ""},
            }
        }

    def to_log_upload_token(self, bucket_endpoint: str = "") -> dict[str, Any]:
        """`dev_upload_log_token` — where to PUT the device's own `devRun.log`.

        A different upload path from `to_oss_sts`, and a much less forgiving one.
        `logUpload` builds the destination from a single format string,
        `https://%s.%s%s/%s` — the scheme is hardcoded, and the authority is
        VIRTUAL-HOST style, `{bucketName}.{endPoint}`, with no path-style
        fallback anywhere in the binary. PetKit's own two values happen to
        concatenate into a public DNS name; ours have to be split out of one LAN
        address by `split_bucket_authority` below.

        Confirmed against 433 captured exchanges with the real cloud
        (2026-07-28) plus the strings in `logUpload`:

        * `result.type` is compared against `"ali"`, which selects the OSS
          branch. Anything else falls through to the Qiniu one.
        * `result.data` carries `token` / `bucketName` / `pathPrefix` /
          `endPoint` / `secret` / `keyId`. All six are literal strings in the
          binary; `to_oss_sts` records what a field the firmware reads and does
          not find costs (a `strncpy` from NULL), so all six are always present.
        * The credentials are shape-correct placeholders, not secrets:
          `http/bucket.py` accepts every upload regardless of the OSS signature,
          so nothing here is checked. They are fixed rather than random so a
          response is stable and testable.
        * The outer `result.key` is the Qiniu object name and is sent for shape
          fidelity. The outer `result.token` is deliberately OMITTED: without it
          the Qiniu branch quits ("qiniu key or token is null, quit qiniu
          upload"), which is exactly the branch we do not implement.

        Returns:
            The token body, or `{"result": {}}` when the bucket address cannot
            be split — the same answer the device already gets today, and one
            its own log confirms it accepts.
        """
        split = split_bucket_authority(bucket_endpoint)
        if split is None:
            return {"result": {}}
        bucket_name, end_point = split
        prefix = f"{DEVICE_LOG_KEY_PREFIX}/{self.petkit_id}"
        return {
            "result": {
                "type": "ali",
                "key": f"{prefix}/{self.petkit_id}_devRun.log",
                "data": {
                    "token": "petkit-local-no-sts-token-required-by-this-bucket",
                    "bucketName": bucket_name,
                    "pathPrefix": prefix,
                    "endPoint": end_point,
                    "secret": "petkit-local-unchecked-secret-000000000000",
                    "keyId": "STS.petkit-local",
                },
            }
        }

    def to_oss_sts(self, bucket_endpoint: str = "", aes_key: str = "") -> dict[str, Any]:
        """`dev_oss_sts_info_new_v2` — where and how to upload media, per capability.

        Protocol notes, reverse-engineered from a real PetKit cloud response
        plus RE of the device's `cloud` binary. Every one of these is a
        constraint the firmware imposes, not a preference:

        * `result.type = "oci"` selects `union_info_parse_oci` on the device.
        * `result.capability` is an ARRAY, one element per media type, and the
          type is named by that element's `cycleType` field.
        * `parse_token_object` reads `cycleType` to assign the upload slot
          (img / event / storage).
        * `primaryDomain` is a FULL URL with a path, NOT just a hostname.
        * `primaryAesKeyStr` is 16 hex chars (8 bytes), NOT 32.
        * The PAR URLs include the full OCI PAR path.

        Args:
            bucket_endpoint: Base URL of our own bucket listener. Empty yields
                an EMPTY capability list — see below.
            aes_key: The 16-character key string whose ASCII bytes the device
                uses for AES-128-CBC (see `media/crypto.py`).

        Returns:
            `{"result": {"type": "oci", "capability": [...]}}`, with one
            `capability[]` entry per ENABLED type — a type toggled off is absent,
            which is what stops the device uploading it at the source.

        With no endpoint the list is empty and the device uploads nothing, which
        is the honest answer. This used to fall back to `https://localhost:9000`,
        and a user running docker-compose got exactly that in every upload URL —
        an address that, resolved on the device, IS the device. Naming somewhere
        unreachable is worse than naming nowhere: the device cannot tell the
        difference until it has tried, and it will keep trying. The endpoint is
        derived from `api_url` now (`config.resolve_bucket_endpoint`), so empty
        means nothing was configured at all.
        """
        if not bucket_endpoint:
            log.warning("No bucket endpoint, so device %d is being told it has nowhere "
                        "to upload media. Set --api-url (or --bucket-endpoint) to an "
                        "address the device can reach.", self.petkit_id)
            return {"result": {"type": "oci", "capability": []}}
        par_base = bucket_endpoint.rstrip("/")
        par_url = f"{par_base}/"
        # primaryDomain goes through sscanf("https://%[^/]/%s") → getaddrinfo(domain).
        # Port in hostname breaks getaddrinfo. Use a portless URL with dummy path
        # so sscanf extracts clean hostname. Actual uploads use primaryParUrl (curl,
        # which handles ports fine).
        host = par_base.split("//")[-1].split(":")[0].split("/")[0]
        domain_url = f"https://{host}/petkit-local/"
        key = aes_key or "0000000000000000"
        far_future = 4102444800  # 2100-01-01
        capability = []
        for ct in self.CAPABILITY_TYPES:
            if ct not in self.enabled_capabilities():
                continue  # toggled off: the device stops uploading this type at the source
            capability.append({
                "deviceId": self.petkit_id,
                "deviceType": 21,
                "cycleType": ct,
                "cycle": 0,
                "cycleExpiration": far_future,
                # Per-device AND per-capability, so raw uploads land pre-sorted
                # in the hidden raw dir (see media/pipeline.py) even before
                # file_info arrives to tell us which capability they belong to.
                "pathPrefix": f"{self.device_type}/{self.petkit_id}/{ct}",
                "primaryAesKeyStr": key,
                "primaryAesKeyUri": "",
                "primaryBucketName": "petkit-local",
                "primaryDomain": domain_url,
                "primaryParUrl": par_url,
                "primaryParExpiration": far_future * 1000,
                "standbyBucketName": "petkit-local",
                "standbyDomain": domain_url,
                "standbyParUrl": par_url,
                "standbyParExpiration": far_future * 1000,
                "standbyAesKeyStr": key,
                "standbyAesKeyUri": "",
                "isHD": 1,
            })
        return {
            "result": {
                "type": "oci",
                "capability": capability,
            }
        }

    def pop_commands(self) -> list[Any]:
        """Drain the queue and return everything in it, oldest first.

        Destructive on purpose: the heartbeat response is the only delivery
        attempt a command gets, so leaving entries behind would re-send them on
        every subsequent poll.
        """
        cmds = list(self.command_queue)
        self.command_queue.clear()
        return cmds

    def to_dict(self) -> dict[str, Any]:
        """The persisted form: identity, MQTT credentials and `config` only.

        Live state (`state`, `command_queue`, the liveness timestamps and flags)
        is deliberately excluded — it is re-derived from the device's next
        contact, and persisting it would resurrect a stale "online" after a
        restart.
        """
        return {
            "device_type": self.device_type,
            "petkit_id": self.petkit_id,
            "serial_number": self.serial_number,
            "mac": self.mac,
            "firmware": self.firmware,
            "mqtt_product_key": self.mqtt_product_key,
            "mqtt_device_name": self.mqtt_device_name,
            "mqtt_device_secret": self.mqtt_device_secret,
            "api_secret": self.api_secret,
            "config": self.config,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Device:
        """Rebuild a device from its `to_dict` form.

        Raises:
            KeyError: `device_type` or `petkit_id` is missing. Everything else
                falls back to a freshly generated default, but those two cannot
                be invented — `DeviceRegistry._restore` drops such an entry
                rather than letting one bad record cost every device its
                credentials.
        """
        d = cls(
            device_type=data["device_type"],
            petkit_id=data["petkit_id"],
            serial_number=data.get("serial_number", ""),
            mac=data.get("mac", ""),
            firmware=data.get("firmware", ""),
        )
        d.mqtt_product_key = data.get("mqtt_product_key", d.mqtt_product_key)
        d.mqtt_device_name = data.get("mqtt_device_name", d.mqtt_device_name)
        d.mqtt_device_secret = data.get("mqtt_device_secret", d.mqtt_device_secret)
        d.api_secret = data.get("api_secret", d.api_secret)
        d.config = data.get("config", {})
        d.created_at = data.get("created_at", d.created_at)
        return d
