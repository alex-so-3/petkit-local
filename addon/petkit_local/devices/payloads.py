"""The wire bodies a device is served: one function per device-facing endpoint.

A device never sees our data structures — it sees JSON bodies on a handful of
device-facing HTTP endpoints, and the firmware is unforgiving about their exact
shape (see `to_signup` and `to_oss_sts`, whose comments record what was learned
the hard way). Every one of those bodies is generated here, next to the state it
is built from, so a field's type cannot drift between the HTTP handler that
serves it and the MQTT service reply that mirrors it.

The `to_*` functions generate the response bodies for the device-facing
endpoints, `{"result": ...}` wrapper included, so a handler stays a
one-liner and the firmware-mandated shape lives next to the fields it is
built from instead of being spread across `http/handlers/`. They are
intended to be pure, and all but one are: `to_device_info` shares (rather
than copies) `config["settings"]` into its result and can write a `k3Config`
key back through it — see its own docstring.
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from petkit_local.devices.base import encode_multi_range, split_bucket_authority
from petkit_local.devices.defaults import default_settings, multi_config_ranges

# Cloud deviceType per codename, confirmed from STS captures. The cloud
# returns this in every capability[] entry. Only models with a capture
# are listed; everything else defaults to 21 (T5).
_DEVICE_TYPE_IDS: dict[str, int] = {
    "t5": 21,
    "d4sh": 25,
}
from petkit_local.devices.state_tables import CONSUMABLE_RECORD_KEY, SPRAY_TOTAL_DAYS
from petkit_local.utils.coerce import to_float
from petkit_local.utils.const import DEVICE_LOG_KEY_PREFIX

if TYPE_CHECKING:  # `devices.ble` imports `devices.registry`, which imports `devices.base`.
    from petkit_local.devices.base import Device
    from petkit_local.devices.ble import BLERegistry

log = logging.getLogger(__name__)


def to_signup(device: Device) -> dict[str, Any]:
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
    ts = int(device.created_at)
    return {
        "result": {
            "id": device.petkit_id,
            "signupAt": str(ts),
            "createdAt": ts,
            "secret": device.signing_secret,
            "mac": device.mac,
            "sn": device.serial_number,
            "timezone": device.timezone_offset,
            "locale": device.config.get("locale", ""),
            "shareOpen": 0,
            "petInTipLimit": 0,
            "p2pType": 2,
            "tooManyPets": 0,
            "frequencyPetTip": 0,
            "deodorantTip": 0,
            "purificationTip": 0,
        }
    }


def to_iot_device_info(device: Device, mqtt_host: str) -> dict[str, Any]:
    """MQTT credentials nested in the Aliyun `ali` envelope — every model but
    `iot_device_info.FLAT_CREDENTIAL_TYPES`, on all three endpoint names."""
    return {
        "result": {
            "ali": {
                "id": device.petkit_id,
                "deviceName": device.mqtt_device_name,
                "deviceSecret": device.mqtt_device_secret,
                "iotPlatform": "ALI",
                "iotInstanceId": device.mqtt_product_key,
                "productKey": device.mqtt_product_key,
                "mqttHost": mqtt_host or device.aliyun_mqtt_host,
                "createdAt": int(device.created_at * 1000),
                "type": 1,
                "regionId": "eu-central-1",
            }
        }
    }


def to_iot_device_info_flat(device: Device, mqtt_host: str) -> dict[str, Any]:
    """The FLAT credential block (no `ali` wrapper), for the ESP32 litter boxes.

    The same fields as `to_iot_device_info`, un-nested. PetKit's cloud answers
    a T3 this way on `dev_iot_device_info`, and a T4 handed the `ali`-wrapped
    block never gets past signup; a D4SH on the same endpoint is answered
    wrapped. Which models get it is `iot_device_info.FLAT_CREDENTIAL_TYPES`.
    """
    return {
        "result": {
            "id": device.petkit_id,
            "deviceName": device.mqtt_device_name,
            "deviceSecret": device.mqtt_device_secret,
            "iotPlatform": "ALI",
            "iotInstanceId": device.mqtt_product_key,
            "productKey": device.mqtt_product_key,
            "mqttHost": mqtt_host or device.aliyun_mqtt_host,
            "createdAt": int(device.created_at * 1000),
            "type": 1,
            "regionId": "eu-central-1",
        }
    }


def to_serverinfo(device: Device, api_url: str) -> dict[str, Any]:
    """`dev_serverinfo` — where the device should send everything from now on.

    `nextTick` is how long the device waits before asking again, and 3600 is
    the real cloud's answer (captured 2026-07-27). A short value here is pure
    cost — 30 works out to ~2800 needless requests a day — because the device's
    own heartbeat is what detects a lost connection, not this poll.

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


def to_device_info(device: Device, ble_registry: BLERegistry | None = None) -> dict[str, Any]:
    """`dev_device_info` — the device's own full configuration, as it sees it.

    Every CAMERA gets the `capacity[]` / `cloudProduct` block that stands in
    for a cloud subscription — a feeder as much as a litter box, because the
    firmware gates cloud storage on it either way. Camera litters additionally
    get the spray/deodorant tip fields, and, when `ble_registry` is given,
    litters get the embedded K3 purifier block (`withK3`, `k3Device`,
    `settings.k3Config`) for whichever K3 is linked to this device.

    The `settings` block is the seeded defaults MERGED UNDER whatever has been
    stored, not one or the other. A settings write records only the field it
    changed (`ha/commands.py` sets a single key), so serving the stored dict
    alone shrank this block from every default to the one key somebody last
    touched — and the device reads it as its entire configuration. The symptom
    is loudest on a fountain, which reports no settings of its own and so has
    nothing to refill the block with.

    NOTE, and unlike its siblings, this method is NOT pure: `k3Config` is
    written into the stored config as well as the answer, so it outlives the
    K3 being unlinked (nothing removes the key again).
    """
    stored = device.config.get("settings", {})
    settings = {**default_settings(device), **stored}
    result = {
        "id": device.petkit_id,
        "mac": device.mac,
        "sn": device.serial_number,
        "secret": device.signing_secret,
        "timezone": device.timezone_offset,
        "locale": device.config.get("locale", ""),
        "shareOpen": 0,
        "modelCode": 2,
        "btMac": device.config.get("bt_mac", ""),
        "settings": settings,
        "multiConfig": True,
        "petInTipLimit": 15,
        "p2pType": 2,
        "serviceStatus": 1,
        "hertz": 50,
    }

    if device.is_camera:
        now = int(time.time())
        far = 4102444800
        # The standing cloud "subscription", and every camera needs it, not
        # just a litter box. `ctrl` parses this into `pkg_service[].workTime`
        # / `.indate` and gates cloud storage on the window still being open,
        # so a camera FEEDER without it records the clip and never stages or
        # uploads it -- the device logs "feed not upload pic and video ..."
        # and every event reports `media: 0`. Two people found that
        # independently, on a D4H and on a D4SH.
        #
        # `indate` is the same far-future stamp `to_oss_sts` uses for
        # `cycleExpiration`, not a real billing window.
        #
        # Mirrors to_oss_sts's capability[] set — a disabled capability
        # must disappear from BOTH so the device doesn't see conflicting
        # answers about what it's allowed to upload.
        result["capacity"] = [
            {"name": ct, "workTime": now, "indate": far}
            for ct in device.CAPABILITY_TYPES if ct in device.enabled_capabilities()
        ]
        result["cloudProduct"] = {
            "serviceId": 0,
            "name": "Local",
            "workTime": now,
            "workIndate": far,
            "chargeType": "LOCAL",
            "subscribe": 0,
        }

    if device.is_litter and device.is_camera:
        result["sprayDays"] = SPRAY_TOTAL_DAYS
        # Falls back to the stamp we recorded, because `state` is empty for
        # the first moments after a restart and the firmware has a setter
        # for this field (`set sprayResetTime (%d)` in `ctrl`). Echoing a
        # zero there would push the N60 countdown's origin back to now on
        # the box itself, silently costing the owner the rest of a
        # cartridge's warning. PetKit's own reply carries the true value.
        recorded = (device.config.get(CONSUMABLE_RECORD_KEY) or {}).get("n60")
        result["sprayResetTime"] = (to_float(device.state.get("sprayResetTime"), 0)
                                    or to_float(recorded, 0))
        result["tooManyPets"] = 0
        result["frequencyPetTip"] = 0
        result["deodorantTip"] = 0
        result["purificationTip"] = 0

    if ble_registry and device.is_litter:
        k3 = ble_registry.get_linked_k3(device.petkit_id)
        if k3:
            result["withK3"] = 1
            result["k3Id"] = k3.petkit_id
            result["k3Device"] = {
                "id": k3.petkit_id,
                "mac": k3.mac,
                "sn": k3.serial_number,
                "secret": k3.secret,
            }
            # Written to the STORED settings as well as to the answer. This
            # used to happen implicitly, by the answer BEING the stored dict;
            # the merge above ends that identity, so the persistence it relied
            # on is now stated instead of inherited.
            k3_config = {"config": k3.config}
            device.config.setdefault("settings", {})["k3Config"] = k3_config
            result["settings"]["k3Config"] = k3_config
        else:
            result["withK3"] = 0

    return {"result": result}


def to_multi_config(device: Device) -> dict[str, Any]:
    """`dev_multi_config` — the schedule ranges (light, do-not-disturb, camera).

    Every value in the result is a JSON-encoded STRING, not a nested object
    (verified against the real PetKit cloud), and each such string wraps its
    own key again.

    A `*MultiRange` comes in TWO shapes and which one a field wants is per
    field, not per model: a flat `[[start, end], ...]`, or an array of schedule
    objects `[{enable, rpt, time}]`. The camera-GATING fields —
    `cameraMultiRange` on a litter box and a fountain, `cameraMultiNew` on a
    camera feeder — take the object form; feeding one a bare pair makes every
    `rpt`/`time` lookup null and leaves the schedule table empty, which reads
    to the device as "no window" and silently disables recording. The mirror
    mistake fails just as quietly: a Format-A field given objects fails its
    `cJSON_IsArray` check on every element.

    The `distrubMultiRange` misspelling is intentional: it is what the
    firmware sends and expects, so correcting it here would silently drop
    the do-not-disturb schedule.

    Every value comes from `multi_config_ranges`, never from a literal built
    inline here. A device polls this endpoint repeatedly, so a made-up light
    period or quiet-hours window would be re-pushed on every poll and would
    undo a schedule the owner set through PetKit's own app in proxy mode.
    """
    return {"result": {
        key: encode_multi_range(key, value)
        for key, value in multi_config_ranges(device).items()
    }}


def to_video_device_info(device: Device) -> dict[str, Any]:
    """`dev_video_device_info` — empty Agora credentials.

    Live view runs over the local RTSP/go2rtc path, so the device must be
    told there is no Agora account rather than being left to retry one.
    """
    return {
        "result": {
            "agora": {"license": "", "appId": ""},
        }
    }


def to_log_upload_token(device: Device, bucket_endpoint: str = "") -> dict[str, Any]:
    """`dev_upload_log_token` — where to PUT the device's own `devRun.log`.

    A different upload path from `to_oss_sts`, and a much less forgiving one.
    `logUpload` builds the destination from a single format string,
    `https://%s.%s%s/%s` — the scheme is hardcoded, and the authority is
    VIRTUAL-HOST style, `{bucketName}.{endPoint}`, with no path-style
    fallback anywhere in the binary. PetKit's own two values happen to
    concatenate into a public DNS name; ours have to be split out of one LAN
    address by `base.split_bucket_authority`.

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
    prefix = f"{DEVICE_LOG_KEY_PREFIX}/{device.petkit_id}"
    return {
        "result": {
            "type": "ali",
            "key": f"{prefix}/{device.petkit_id}_devRun.log",
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


def to_oss_sts(device: Device, bucket_endpoint: str = "",
               aes_key: str = "") -> dict[str, Any]:
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
    is the honest answer, and there is deliberately no fallback address. A
    literal like `https://localhost:9000` would reach every docker-compose
    install as its upload URL — an address that, resolved on the device, IS
    the device. Naming somewhere unreachable is worse than naming nowhere: the
    device cannot tell the difference until it has tried, and it will keep
    trying. The endpoint is derived from `api_url`
    (`config.resolve_bucket_endpoint`), so empty means nothing was configured
    at all.
    """
    if not bucket_endpoint:
        log.warning("No bucket endpoint, so device %d is being told it has nowhere "
                    "to upload media. Set --api-url (or --bucket-endpoint) to an "
                    "address the device can reach.", device.petkit_id)
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
    for ct in device.CAPABILITY_TYPES:
        if ct not in device.enabled_capabilities():
            continue  # toggled off: the device stops uploading this type at the source
        capability.append({
            "deviceId": device.petkit_id,
            "deviceType": _DEVICE_TYPE_IDS.get(device.device_type.lower(), 21),
            "cycleType": ct,
            "cycle": 0,
            "cycleExpiration": far_future,
            # Per-device AND per-capability, so raw uploads land pre-sorted
            # in the hidden raw dir (see media/pipeline.py) even before
            # file_info arrives to tell us which capability they belong to.
            "pathPrefix": f"{device.device_type}/{device.petkit_id}/{ct}",
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
