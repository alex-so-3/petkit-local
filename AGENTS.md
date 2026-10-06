# petkit-local

A Home Assistant add-on that impersonates the PetKit cloud: a litter box, feeder or fountain
is pointed at it instead of PetKit's servers (the Pura Air purifiers are BLE-only and reach it
through a parent device), and petkit-local answers as the official API, stores events and media
locally, and publishes entities via MQTT discovery.

A device speaks HTTP **or** MQTT, not both: it starts on HTTP and stops polling the heartbeat once
it reaches the broker. Neither transport is dedicated to a kind of message — see `ARCHITECTURE.md`.

It is an add-on **repository** — `repository.yaml` at the root, the add-on in `addon/`, the package
in `addon/petkit_local/`. It also runs as a plain container or bare process (`docker-compose.yml`
at the root, or `--no-ha`); HA Container and HA Core have no add-on system, so that path is
supported, not a fallback.

**Stack.** Python 3.11+, one asyncio loop, one container. aiohttp (device API, bucket, panel), amqtt
(embedded device-facing broker), aiomqtt (client for HA's broker), SQLAlchemy 2.0 async + aiosqlite
(`{data_dir}/petkit.db`), Jinja2, ffmpeg. Device identity and settings persist as atomic JSON
(`devices.json`, `ble_devices.json`). `README.md` credits the projects the payloads came from.

## Orientation

`ARCHITECTURE.md` is the map: what each package owns, and how a device request travels through
them. Read it first — this file assumes it.

Two rules about placement that the map does not make obvious:

- `devices` depends only on `events` and `utils`. "Which entities does this device publish" is an
  HA question, answered in `ha/categories.py`. Do not answer it from `devices/`.
- `events/codes.py` and `devices/payloads.py` carry almost all the reverse-engineered protocol and
  are deliberately large. Everything below is about not breaking them.

## Invariants — do not break these

- **All protocol knowledge lives in `events/codes.py`** — seven namespaces, each row graded
  (`confirmed`/`inferred`/`unverified`/`conflicted`) and carrying the firmware function behind it.
  Add a code there, not in a private set somewhere: the nine parallel collections this replaced are
  exactly why seven real codes were classified by none of them. `events/decode.py` renders values;
  `events/normalize.py` only does transport.
- **HTTP `event_type` codes are PER DEVICE CATEGORY, not global.** Code `2` is `err_over` on a
  litter box and `feed_over` on a D4H feeder. Always pass `device_type` to `codes.lookup` /
  `classify_event_kind` / `event_label`; omitting it assumes a litter box.
- **Two event-code namespaces, never merged.** Our `dev_event_report` codes are authoritative here;
  the cloud-record API's `LitterRecord.subContent[].eventType` ints overlap on 5/8/10 with
  *different* meanings, so only namespace-independent sub-field decoding (`result`/`start_reason`/
  `err`) is borrowed. NS2 stays quarantined in `codes.CLOUD_RECORD_TYPES`, read by nothing.
- **The capture wins on SHAPE, the firmware RE wins on MEANING.** Where they disagree the row is
  graded `conflicted` and its `note` says how — never pick silently. The repo's original labels
  were neither source: code `8` read "Cleaning started" but is the deodorizing cycle's completion.
- **Days are cut at LOCAL midnight** (`utils/timeutil.local_day_bounds`), and the end of a day is
  midnight-next-day re-localized, never `start + 86400` — DST makes days 23 and 25 hours long.
- **`event_id` is a SESSION key** shared by several distinct reports of one visit; it becomes
  `related_event`, and the dedup key is `event_id + event_type` — deduping on `event_id` alone
  loses every report of a session but the last.
- **`petEvent` alone is not a toilet visit** — only `toiletEvent` means the box was used. Code `20`
  is a pet-appeared episode (`petEvent=1`, `toiletEvent=0`, no weight, no cleaning cycle) that the
  app counts under "Pet", never "Toileting".
- **A state report is a FIXED 29-key dump plus 3 optional keys, and absence is the signal.**
  Measured over 1254 real snapshots across both transports: 29 keys in every single one, and only
  `workState` (166), `lightState` (166) and `refreshState` (32) come and go. Those three are sent
  ONLY while the thing they describe is happening and carry no "off" value — so they must be turned
  into a real 0/1 (`state_parsers.PRESENCE_FLAGS`), because `device.state` is merged into and never
  pruned and a key that stops arriving keeps its last value forever. `refreshState` is also an
  OBJECT, and a non-empty dict is truthy, so a binary sensor bound straight to it latches on at the
  first spray and never returns. Gate any such derivation on `SNAPSHOT_MARKER`: reading absence as
  "off" is exactly as wrong as reading presence as "on" if the payload was never going to carry the
  key. Same rule gives `workState` — a default of 0 there is `WORK_MODES[0] == "cleaning"`, which
  had an idle box reporting itself as cleaning 79% of the time.
- **Every MQTT `params` carries the transport envelope with the telemetry** — `XDevice`, `event_id`,
  `timestamp`, `content`, `state` (`ingest.MQTT_ENVELOPE_KEYS`). `XDevice` is the signed request
  credential. Strip it with `telemetry_only()` before anything merges `params` into `device.state`,
  which the panel renders verbatim.
- **`CLOUD_STORAGE` and `CLOUD_DOUBLE` cover the same timespan and must never be joined** — hence
  the stitch episode key `(device_id, related_event, category)`. A stitch deletes its sources, so
  the joined output is verified before anything is removed (`media/stitch.py`).
- **moduleType -> role -> capability**; roles share capabilities, so never assume role == capability
  (`_MODULE_TYPE_TO_CATEGORY` / `CATEGORY_TO_CAPABILITY`, `events/normalize.py`):

  | moduleType | role | capability | folder |
  |---|---|---|---|
  | `CLOUD_STORAGE` | fullVideo | fullVideo | Playback — main stream, ~4s chunks |
  | `CLOUD_DOUBLE` | cloudDouble | fullVideo | Timelapse — ~4x, silent, low-res |
  | `EVENT_VIDEO` | dynamicVideo | dynamicVideo | Clips — the app's "Highlight" |
  | `EVENT_PREVIEW` | eventImage | eventImage | Snapshots — one poster per event |
  | `SHIT_PICTURE` | wasteCheck | eventImage | Waste — the "check waste" gallery |
  | `HEALTH_PRED` | healthPic | eventImage | Health — stool analysis |

- **A K3 is in three payloads and in none of them is it a relay entry.** It is excluded from
  `dev_ble_device` on purpose — listing it there makes the parent treat it as a second, unpaired
  device. Its binding is `dev_device_info`'s `k3Device`, its parameters are `settings.k3Config`
  (six keys, each named in T4 `_pk_WAN_parse_item_k3_info`), and its own detail is
  `dev_k3_device_info`. That parser looks every key up individually, so an absent one is skipped
  rather than read as zero — which is what makes it safe to send only what we know. `relateT4`
  stays out until somebody reads the branch consuming it: parent id and 0/1 flag are both
  plausible, and the wrong one lands on the firmware's `diff ID` path.
- **We ask PetKit only when a button is pressed, and we sign as the device to do it.** The account
  holds things nothing else does — an accessory's pairing `secret`, a pet's reference photos — and
  `http/cloud_fetch.py` asks for them directly rather than waiting to overhear a proxied reply. The
  `X-Device` signature is T4 firmware 1.652's own two format strings, `id%snonce%stimestamp%utype%s%s`
  hashed and `id=%s&nonce=%s&timestamp=%u&type=%s&sign=%s` sent; PetKit answers `704` to anything
  else. Two inputs must come from the device, never from us: `signing_secret` (ours is accepted only
  by us) and `Device.wire_type`, the `type` spelling the firmware itself uses — it is hashed, so
  `T5` and `t5` are different requests. It is recorded from live traffic and persisted, because a
  restart that lost it would sign the next fetch with our lowercase codename and be refused.
  **This must never become a poller**: nothing in the add-on may reach PetKit on a schedule of its
  own. A pet imports under a NEW local id with PetKit's id bound as a `device_pet_ids_json` alias,
  because that foreign id is what a box matching against cloud-cached faces actually reports
  (`resolve_pet_ref`); `bind_pet_ref` then names the history retroactively.
- **Never 404 a device.** Unhandled endpoints fall through to `handle_catchall`; firmware reads 4xx
  as a server fault and retries forever. The `/{path:.*}` route stays registered last. Proxy mode
  holds the same line from the far side: an upstream that is unreachable, slow, or that REFUSES
  (a device the real cloud never heard of gets 401 on everything) falls back to the local answer.
- **Proxy mode never hands a device an upstream credential, server address or firmware.** Redaction
  is content-keyed, not endpoint-keyed (`http/redact/`), so a hostile field is caught on an
  endpoint nobody expected it on. Only *blocked* rules are persisted — the address, MQTT, STS and
  timezone rewrites fire on routine polling, so recording each one would bury the attempts that
  matter. Proxy mode and capture are configured ONLY from the panel; no add-on option, no CLI flag.
- **Proxy mode LEARNS settings, it never changes what is relayed.** A device that reports no
  settings of its own (a W7H) is only ever told them by the cloud, so `ha/learn.py` records the
  fields of a relayed `thing.service.property.set` (`mqtt/upstream.py`) and of a served proxied
  `dev_device_info` `settings` block (`http/middleware/proxy.py`) exactly as an HA write would, from
  the REDACTED body (MQTT: after the frame is published; HTTP: before the reply is returned, which
  it never changes). Allow-listed to `settings.` fields the device's own entities read plus the
  `*MultiRange` windows `schedule_targets` offers; `timezone` is never learned
  (`Device.timezone_offset` has its own precedence). A `property.set` overwrites; a
  `dev_device_info` only fills fields never stored, because the account still holds the old value
  of anything set from HA while proxied. Applies to every model; only the W7H depends on it.
  Nothing is learned with proxy mode off.
  A cloud `user/get` push and a proxied `dev_multi_config` are NOT learned from yet: neither shape
  has been captured for this purpose.
- **A heartbeat delivering a command is never forwarded in proxy mode.** `pop_commands` is
  destructive and at-most-once and has already run by the time forwarding could start, so ANY await
  between the pop and the send can lose the command — and `wait_for_heartbeat`, which watches the
  queue drain, then reports it delivered. `handlers/heartbeat.py::carries_commands` is the gate.
- **A device on MQTT STOPS polling the HTTP heartbeat.** Confirmed on a T5: it went quiet over HTTP
  ~40s after its CONNECT and stayed quiet for as long as the session lived. So liveness may never be
  judged on HTTP timestamps alone — `mqtt/auth.py::on_mqtt_packet_received` stamps `last_mqtt` from
  every packet the broker reads, PINGREQ included, and `device_is_stale` counts it. Get this wrong
  and the watchdog calls the healthiest device offline, clears `mqtt_connected`, and routes its
  commands to a queue the device is no longer polling — the failure that needed an add-on restart.
- **`mqtt_connected` picks the transport and the transport has no delivery report.** Publishing to a
  topic with no subscriber raises nothing, so a stale True does not fail — it swallows commands. It
  goes up only on an authenticated CONNECT and comes down three ways:
  `on_broker_client_disconnected` (immediate), the heartbeat's `?iotStatus=0` (backstop for a
  session that ends without the broker noticing), and the offline watchdog (last resort). The
  device's client id carries `timestamp=`, so a returning device is a NEW client, never a take-over
  — which is why the disconnect hook compares `Session` objects instead of trusting the client id.
- **A T5 sends no SUBSCRIBE — the broker subscribes it, or it receives nothing.** Confirmed on
  hardware: a session that authenticated, PINGREQ'd and posted telemetry for minutes held an empty
  subscription list, so every `thing/service/*` publish — ours and every one proxy mode relayed
  down from the real cloud — was accepted by the broker and dropped. `mqtt/auth.py::_server_subscribe`
  adds `topics.downstream_filters` on `CLIENT_CONNECTED`, at QoS 0 (what we publish; a higher value
  could deliver a QoS-1 message this firmware may never ack). The OTA topic is deliberately NOT among
  them. `Device.mqtt_subscriptions` makes it visible, because a publish to a filter nobody holds
  looks exactly like one that was obeyed.
- **A `_reply` travels the opposite way to the topic it answers.** The device acknowledges a
  `thing/service/{name}` on `thing/service/{name}_reply`, so `topics.is_server_published` must not
  claim it — classifying it as ours dropped the only evidence a command ever arrived. (Our own
  `thing/event/{t}/post_reply` never reaches that check: `EVENT_PATTERN` anchors on `/post`.)
- **Entity keys are user state** — renaming an `EntityDef` orphans live entities. HA's silent-
  rejection rules (select labels, `text` max 255, `| default(...)`, `'None'`) are in `ha/discovery.py`.
- **Device input never raises.** Untrusted scalars go through `utils/coerce`, untrusted path
  components through `utils/paths.safe_join` — `http/bucket.py` listens unauthenticated on 0.0.0.0.

## Dev commands

```sh
cd addon && pytest                   # the whole suite, no device or broker needed
cd addon && pytest --firmware        # also the patcher tests, if tests/firmware/ is populated
ruff check petkit_local/ tests/      # narrow lint gate; CI runs it too
# The panel's CSS/JS are prettier-formatted (.prettierrc at the repo root).
# CI checks this, so run it before pushing rather than after:
npx prettier@3 --write addon/petkit_local/web/static/js/*.js addon/petkit_local/web/static/styles.css
pytest tests/test_media_stitch.py    # one module
# deploy to a HAOS box — code/Dockerfile only. If config.yaml itself changed, the Supervisor
# caches it by mtime: rsync, `touch` it, `ha supervisor restart`, `ha addons update <addon>`.
rsync -av --delete --exclude='__pycache__' --exclude='*.pyc' \
  addon/ root@<HAOS-HOST>:/addons/petkitlocal/
ssh root@<HAOS-HOST> 'chown -R root:root /addons/petkitlocal && ha addons rebuild local_petkitlocal'
```

**NEVER deploy an untested Dockerfile.** The Supervisor deletes the old image before rebuilding, so
a build that fails leaves NO image: the add-on drops to `state: error` and the devices lose their
local cloud until it is fixed. Build it first with plain `docker build` in `/tmp` on the box, then
rsync. Recovery is restore-known-good, `ha addons rebuild`, then `ha addons start` — rebuild alone
does not restart it.

**`build.yaml` is dead** (removed in Supervisor 2026.04.0) and so is `ARG BUILD_FROM`: the Supervisor
substitutes its own base image, one with no Python, and the build dies with `pip: not found`. The
Dockerfile is the single source of truth now — hardcode `FROM`, and key anything per-arch on
BuildKit's own `TARGETARCH` (which is what the 32-bit-ARM glibc base does).

## Known limitations / unverified

- **MQTT-side, a real T5 has now connected — the transport is confirmed, the semantics are not.**
  Settled on hardware: TLS CONNECT with the `mqtt` patcher applied, `keep_alive=62s`, and an
  HMAC-SHA256 signature that matches ours exactly except that the device sends it in UPPERCASE hex
  (compared case-sensitively it fails every CONNECT, invisibly, until `mqtt_strict_auth` locks the
  device out). `property` events arrive and round-trip. Still unconfirmed: the semantic event types
  (`pet_in`, `clean_over`, …) `mqtt/bridge.py` and `ingest.from_mqtt` dispatch on — a wrong guess
  degrades to `event_kind="other"`. The device offers only RSA and PSK cipher suites, so the broker
  sets `DEFAULT:@SECLEVEL=0` (`mqtt/broker.py`) — Python 3.12's default context drops plain RSA and
  the handshake finds no shared cipher.
- **`mqtt/upstream.py` has now reached the real Aliyun broker, and its TLS is NOT authenticated.**
  The CONNECT is accepted, which confirms port 8883, `securemode=2`, the client-id shape and
  `build_credentials` at once. The endpoint chains to `CN=Aliyun IoT Root CA`, a private root in no
  public store, never sent, with no AIA to fetch it from — so the default context failed every
  connection with CERTIFICATE_VERIFY_FAILED. `build_tls_context` therefore encrypts without
  verifying. The device is in the same position (`/app/bin/ctrl` embeds only GlobalSign Root CA,
  which does not validate this chain, and the T5 rootfs holds no copy of the Aliyun root), so this
  matches the firmware rather than relaxing something it enforces — but anything on the path can
  pose as the cloud and have its commands relayed down. OTA stays blocked and redaction still
  applies, neither resting on server identity.
- **SOME settings writes ARE observable in telemetry** — `property/post` carries more than sensors.
  A T5 answered a `property.set{timezone}` with a `property/post` carrying the new value ~30s later
  (verified on hardware 2026-08-12), and the 2026-08-11 four-family capture shows the same frame
  carrying `locale`, `sandType`, `cameraPosition`, `wtLock`, `sprayResetTime` and the whole
  `package*` block. Which fields come back is per family and largely unmapped, so a write that
  produces no echo is not proof it failed. In proxy mode `dev_device_info` is answered by the real
  cloud rather than the device, so that route confirms nothing either way. Note the raw
  `{"suffix","payload"}` panel escape hatch publishes VERBATIM: a command sent that way lacks the
  `method`/`id`/`params` envelope and the firmware has nothing to dispatch on. Use the `entity`
  form, which builds it (`ha/commands.py`).
- **State parsing** is confirmed against real T4/T5 captures; spellings for other codenames come
  from pypetkitapi and localkit. Capture mode (panel: Setup -> Settings) collects
  what is needed to fix one.
- **Protocol soft spots** (all graded in `events/codes.py`, all visible in the panel's Debug info):
  codes `4`/`13` have no firmware name and are labelled from their work mode; the `key` field the
  RE gives `8`/`17` appears in none of our 68 captures. (`17` is settled: it is the LED
  illuminator, once per recorded episode -- see its note in `events/codes.py`.) `highLight` has no
  observed `moduleType`.
- **The AI chain is settled, and its two `score`s are not the same number.** `dev_discern_config`'s
  `score` (25) is the BODY-DETECTION floor deciding whether an episode opens at all;
  `content.score_info[].score` is a face-match similarity running to 1846. Never compare them —
  that confusion is how the endpoint was misread for a year. `area` (6000) IS a floor on the
  detected bounding box in detector pixels, and both values persist to device flash
  (`user.conf` `other.minArea`). Confirmed in `ctrl`: `get_pet_discern_config_by_network`,
  `get_update_face_score_info`.
- **`score_info[].id` is the pet id we handed out**, copied verbatim from `dev_discern_pic`'s outer
  `list[].id`. It lands in `events.pet_ref`; only `PetRegistry.resolve_pet_ref` promotes it to
  `pet_id`, because a box still holding faces cached from PetKit's cloud reports THAT cloud's id.
  Writing an unresolved ref into `pet_id` fabricates an HA pet device for a row that does not exist.
- **`discernPic` in the state report is a readback**, not a config echo: the `discern[].id` values
  the device downloaded and feature-extracted (features persist in `/system/feature.bin` there). It
  is the only proof a `dev_discern_pic` payload was accepted, and the first thing to check when
  recognition does not work.
- **Camera `still_image_url`** needs the device IP from a state report plus a rooted Ingenic device
  running `tserver`. **Video thumbnails need ffmpeg**; image thumbnails are the original file.
- **The device has no timezone of its own, and there are TWO ways to give it one.** BLE
  provisioning (`ssid:%s pwd:%s hide:%d locale:%s timezone:%f`) is the path that survives being
  the only one used, so a device provisioned before that field was sent burns UTC into video
  watermarks. But `property.set{timezone}` also works at runtime, and the value **must be a JSON
  STRING**: `parse_recv_property_set_normal` reads `cJSON.valuestring` and calls `atof()`, so a
  JSON number leaves `valuestring` NULL and the write is a silent no-op. Verified on a live T5
  (2026-08-12): the number changed nothing for minutes; `"5.75"` moved the video watermark within
  ~30s. The handler is present in T5 and D4SH (MIPS) and W7-262863 (ARM), where `timezone` is the
  only field in a whole run of `===== set X (%d) =====` handlers read as `%s`. Readback is LOSSY —
  the device reports its own `%.1f`, so `5.75` comes back as `5.8`. HTTP `dev_device_info` still
  wants a NUMBER for the same field.
- **K2/K3 entity definitions assume a WiFi purifier** that does not exist — both models are
  BLE-only today.
