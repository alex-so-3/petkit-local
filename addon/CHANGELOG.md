# Changelog

## Unreleased

- **Proxy mode now teaches Home Assistant the settings you change in PetKit's
  app.** A W7H reports none of its settings, so its controls only ever showed
  what was last set from HA. With proxy mode on, every setting the cloud sends
  the device (`property.set` over MQTT, and the `settings` block of a proxied
  `dev_device_info`) is now recorded the same way an HA write is, and the
  entities and the panel's schedule editor update, do-not-disturb windows
  included. Only fields the device has a control for are kept, the cloud's
  `timezone` is ignored, and the device receives exactly what it did before.
  `dev_device_info` only fills settings never set, so it cannot undo a change
  made from HA while proxied. This applies to every model, though only the W7H
  needed it. Each learned value is logged
  (`learned <field>=<value> from ...`). Nothing changes with proxy mode off.

## 2.1.0 — 2026-08-12

The YumShare Dual-Hopper (D4SH) camera feeder is now confirmed working, and most
of this release is what that took: real feed schedules, custom sounds, its camera
recording, its event codes, and a pile of routing and response-shape fixes found
against live captures. It also adds two-way talk — the panel's microphone to the
device speaker — and models the single-hopper YumShare Solo (D4H) as its own
thing rather than a Dual-Hopper with a hopper removed.

- **Two-way talk (intercom).** A new device patcher installs a local audio sink
  so the panel's microphone can reach the device speaker — real two-way audio
  without PetKit's cloud, which routed talkback through Agora. Listening already
  came from the local camera stream; this is the missing half. The panel
  transcodes the browser mic to the device's format and streams it to a small
  listener the patch installs, which hands it to the firmware's own audio player.
  Verified end to end on a live D4SH. Half-duplex by design (the echo
  cancellation lived on the cloud path), and the listener is unauthenticated on
  the LAN while the patch is applied — apply it on a trusted network only.

- **The YumShare Solo (D4H) is modelled as a single-hopper feeder** rather than a
  Dual-Hopper with one hopper taken away. It gets one hopper-level sensor reading
  its singular `food` field and keeps the family's Food Low sensor, where the
  Dual-Hopper gets two hopper levels and per-hopper feed controls. This is
  inferred, not measured — no D4H has ever reported to the add-on — so it bets
  the device reports like a plain single-hopper feeder; every guessed site says
  so in the code and points back to the Dual-Hopper shape for when one is
  captured.

- **HTTP event codes are read per device category.** The same `event_type`
  number means different things on different hardware, so it is now mapped per
  category: feeder `3`/`4` (feed started/over) and the camera-feeder snapshots
  `5` (eating started), `7` (motion) and `8` (pet appeared); fountain `5`/`6`/
  `20`/`24`; and litter BLE-relay `51`/`53`. The feeder snapshot numbers are
  graded inferred — the firmware does not carry the number as a literal, so they
  were read from the recordings attached to each event, not confirmed against the
  binary. A value with no mapping stays a number rather than borrowing another
  category's label.

- **Devices can be deleted from the panel**, with their Home Assistant entities
  cleaned up instead of left orphaned. A device's IP is now learned from its
  event and state reports — which is what the patcher and camera features need to
  reach it — and event snapshots appear in the panel's live log.

- **A camera feeder stopped recording its feeds.** 2.0.x renamed the recording-
  window field it serves from `cameraMultiNew` to `cameraMultiRange`, on the
  theory that `dev_multi_config` used a different key than `dev_device_info`.
  It does not: the D4SH parses the window with `pk_parse_cameraMultiNew_func`,
  which keys on `cameraMultiNew` and stores it internally as `cameraMultiRange`
  — so serving the internal name reached no parser, the window stayed empty, the
  camera never armed (`cameraStatus` 0), and every feed reported `media: 0` and
  uploaded nothing. Restored to `cameraMultiNew`; confirmed live on a D4SH that
  the camera re-arms and feeds record and upload again. Litter boxes and the W7H
  fountain keep `cameraMultiRange`, which is right for them.

- **A feeder's scheduled meals fired two hours late (or on the wrong day)
  because we told it the wrong timezone.** A device that only ever got a zone
  NAME over BLE (`locale: "Europe/Warsaw"`) and never a numeric offset reports
  that offset as `0`, so a box plainly in Warsaw told us it was in UTC — and we
  served UTC straight back in `dev_device_info`, leaving its clock two hours
  behind. The offset is now recovered from the zone name the device itself
  reports, which is unambiguous and — unlike a number frozen at provisioning —
  stays correct across DST. Separately, the panel's timezone push only ever
  went over MQTT, so for a feeder that talks HTTP it was a silent no-op; it now
  falls back to the heartbeat queue like every other command, and the value
  stays a JSON string (a number is a no-op in the firmware's parser).

- **Feeder meal times were being sent in the wrong unit — a meal set for 18:02
  fed the cat at 00:18.** A meal's `t` counts SECONDS since local midnight on
  the wire (the cloud's `n_46560` fires at 12:56:00 — confirmed against 21 real
  responses captured 2026-08-12); the schedule editor stored MINUTES. Schedules
  saved with the old editor are converted once, automatically. `dev_feed_get`
  now also mirrors the cloud's bookkeeping exactly: `latest` lists only feeds
  firing today or tomorrow, `nextTick` is the last listed feed's countdown (or
  the cloud's 86340 constant when nothing is coming up), and `itemJsonString`
  is serialized with the cloud's own key order — so what the feeder fetches
  after a schedule change is now indistinguishable from the cloud's answer.

- **A device that has never had a PetKit id can now register.** A device is
  given its id by PetKit at first registration and repeats it forever after;
  one that never reached PetKit has nothing to repeat, and was answered with
  a 400 it would retry against forever. Second-hand units sold as "broken"
  are often exactly this. It now gets an id derived from its MAC — the same
  one every time, so a retry does not register a second device, and it
  survives a lost `devices.json`. Ours open with `4` where PetKit's open
  with `3`, so the two can never be confused. A blank serial number is fine
  too. Only a device offering no stable identifier at all is still refused.
- **Reset N50 from the panel now records the replacement date.** The panel and
  Home Assistant reached the same button through two different code paths, and
  only Home Assistant's recorded anything. The N50 has no field in any device
  report, so that record is the countdown's only possible source — pressing the
  panel button sent the device command and changed nothing visible. Both
  request forms now go through one handler, and a test holds them to the same
  side effects.
- **A cleaning cycle that stops because the bin is full no longer reads
  "canceled (kitten mode)".** `result: 4` is a full bin — it appears only
  alongside `err: "full"`, on two device families. The kitten-mode label had
  borrowed that slot as a lookup constant for a different case (`result: 3`
  with a `kitten` flag), which still reads correctly.
- `result` values outside the cleaning table are no longer given a cleaning
  cycle's vocabulary. The same field carries a different meaning per event —
  a BLE relay's `1` is not "terminated" — so an unmapped value stays a number.
- Proxy mode no longer confuses two different things both called `capability`:
  the upload credentials in an STS response and the cloud-storage window in
  `dev_device_info`. Matching on the key alone replaced one with the other.
- New proxy option, off by default: **Keep recording locally**. An expired
  PetKit subscription arrives looking like a valid configuration and silently
  stops a camera recording; this answers with our own standing window instead.
  Off by default because an expired window is a fact about someone's account,
  and overriding it is a decision rather than a transport detail.
- **A Purobot Ultra now reports its bagging mechanism.** Six fields it sends in
  every single state report were read by nothing — bag state, packing, bagging,
  seal door, bin store and bags left. They are published raw and marked
  diagnostic, because nothing names what their values mean and a label here
  would be invented. `t6` only; no other litter box has the hardware.
- The add-on now says in its log where it keeps persistent state, or warns
  loudly that it is keeping none. That distinction is invisible until a restart
  loses something, and the patcher list is the part that cannot be rebuilt from
  the device afterwards.
- The contributing guide's warning about captures now names what is actually in
  them: a Purobot Ultra puts a cartridge credential in every state report, every
  camera event carries the key its media is encrypted with, and the Wi-Fi block
  rides along with every event rather than only the state reports.
- **A W7H's schedules exist.** The fountain was served an empty
  `dev_multi_config` and so had no schedule editor at all — the card simply did
  not appear. It now gets the seven ranges its firmware reads, five of them
  confirmed by watching PetKit's own cloud write them. Two more are real fields
  the firmware knows and no capture has ever shown a value for, so they are
  deliberately still not sent: this reply is repeated on every poll, and an
  invented window would overwrite the owner's on every one of them.
- The fountain's drain and flush times moved into the Schedules card, next to
  the periods. They were under Controls, which described where we store them
  rather than what they are.
- **A device's timezone can now be set from the panel, and it actually moves the
  clock.** Until now a box provisioned before the Bluetooth payload carried a
  timezone burned UTC into its video watermarks until it was provisioned again;
  the documentation said that was the only way. It is not — a `property.set`
  does it at runtime, provided the value is sent as a **string**, which is what
  the firmware's parser reads. Verified on hardware: as a number nothing
  happened; as a string the watermark moved within half a minute. Devices tab →
  Timezone. The device reports back to one decimal, so 5.75 reads as 5.8.
- **"Times Dispensed" and "Total Dispensed" finally hold a value.** No feeder
  reports those totals — on PetKit's own service the cloud sums them from the
  feed events, so being the cloud means doing the same. They count per day,
  following the device's own reading of the date, and a jammed feed that
  dispensed nothing counts as nothing. A dual-hopper reports per hopper, and
  both are counted. They also carry a statistics class now, so a midnight
  rollover is not recorded as the value falling.
- **"Desiccant Days Left" counts down.** The sensor and its Reset button both
  existed and nothing connected them; the pack has no representation in the
  device protocol, so the date we record is the only possible source.
- **A camera feeder should now upload what it records.** Three separate things
  stopped it, all reported independently from a D4H and a D4SH: the block that
  tells a device its cloud storage is active was sent only to camera litter
  boxes; the schedule that gates recording was sent in the wrong one of the two
  shapes that field family uses, so the device kept `camera: 1` while logging
  "camera not enable"; and the three upload enables a feeder needs were never
  seeded, which the firmware reads as off. Panel switches (below) push them to a
  feeder that is already running. Confirmed live on a D4SH: with the recording
  window fixed the camera re-arms and feeds upload again.
- **The CA patcher no longer leaves stale copies of our certificate behind.**
  Re-applying it used to be an error; now it replaces. That matters because the
  device anchors on the FIRST certificate whose subject matches, so an old copy
  in front of the current one fails verification while the right one sits
  further down, unread — indistinguishable from a broken certificate.
- The TLS certificate now names the address devices are actually told to upload
  to, not just the host's own detected IP. Those agree on a plain install and
  part company behind a reverse proxy, on a multi-homed host, or when the
  address is configured as a name. An existing certificate is never re-issued
  silently — that would invalidate the copy already patched into every device —
  but the add-on now says so in the log.
- A Timeline filter chip is hidden while it has no events and is not the one
  selected. With eight of them a row of permanent zeros ran off the edge of
  the card.
- **A feeder's and a fountain's events now form cards.** Grouping starts at the
  events allowed to head one, and feeding and drinking were not among them — so
  every meal arrived as three unrelated rows (`feed_start`, `feed_over`,
  `eat_over`) instead of one card, on both transports. The session id that ties
  them together was already correct and simply unused.
- **Feeding, Drinking and Cleaning are filter chips.** They belonged to no
  bucket at all, so those cards showed under "All" and were reachable from no
  other filter — a water refill could only be found by scrolling past it.
- **A card is headed by the time the device says, not the time we received it.**
  Only a toilet visit carries the field that was being read, so every other kind
  of card showed its arrival: on a reported T6, 14:20:19 for something the
  device and its own video watermark both put at 14:19:58.
- **The Debug view's timestamps follow the browser, like everything else on the
  page.** They were formatted in the container's timezone while the card above
  them used the reader's, so an install whose container runs UTC showed one
  instant twice, two hours apart, in adjacent rows.
- The two W7H time-of-day fields step by a minute instead of a second; every
  value they have ever carried is minute-aligned.
- **The Timeline no longer rebuilds itself out from under you.** It refreshes
  400 ms after every event and media frame, and it used to do that by replacing
  the entire view — so on an install with several talkative devices a playing
  video restarted constantly, "show N more steps" collapsed the instant it was
  opened, and the device dropdown closed under the pointer before anything could
  be selected. Cards are now reconciled by id and left alone when their contents
  have not changed, a video whose source is unchanged is carried across rather
  than re-created, and the filter counts are written only when they differ.
- **Changing one setting no longer erases the rest.** A write records only the
  field it changed, and both the device's `dev_device_info` answer and the Home
  Assistant state document substituted that stored dict for the seeded defaults
  instead of merging. So the first change to any setting cut the block the
  device reads as its whole configuration down to a single key, and left every
  other switch, number and select in Home Assistant reading "unknown". Worst on
  a fountain, which reports no settings of its own to refill it with.
- **`dev_upload_file_info` (non-v2) is now routed.** The D4SH calls this
  endpoint alongside v2 (4 requests in a capture), and it was falling through
  to the catch-all — so those media uploads never reached the pipeline.
- **`dev_iot_device_info` returns the ali-wrapped format.** The cloud returns
  `{ali: {...}}` for every device, including those calling the "ESP32" endpoint.
  The previous flat format was an assumption from localkit that no capture
  supported. D4SH reads `result.ali.mqttHost`, so flat = null MQTT host.
- **`dev_feed_get` nextTick is now relative.** Was `now() + 7200` (an absolute
  timestamp the device added another `now()` to, yielding ~56 years). The cloud
  returns `86340` (relative seconds). Fixed to `7200`.
- **`dev_attire_over` returns `1`, not `[0]`.** The real cloud returns an
  integer; the array was copied from an older unverified capture.
- **STS `deviceType` is per-device.** Was hardcoded 21 (T5); the cloud returns
  25 for a D4SH.
- **Camera feeder now has individual media switches.** `Enable Feed Video` (a
  one-way button) is replaced by `Feed Picture`, `Eat Video`, `Voice Prompt`,
  `Quiet Voice Prompts`, and `Do Not Disturb` — each confirmed as independent
  0/1 in a D4SH proxy capture.
- **`FEED_RESULT` codes 3 (blocked) and 11 (nothing dispensed) are mapped.**
- **Feeder error flags are labelled.** `blk_f` now reads "Food outlet blocked"
  instead of the raw firmware abbreviation. All 8 D4SH `err{}` keys are named.
- **12 missing feeder settings seeded** from a D4SH cloud capture, and
  `factor: 10` removed (cloud sends none).
- **Custom sound upload.** Camera feeders can upload audio files through the
  panel, select and play them on the device. `dev_sound_get` serves them with
  local download URLs instead of the previous empty list.
- **Feed and cleaning schedules sync from proxy mode.** A `property.set{feed}`
  or `property.set{schedule}` from the cloud is now stored locally and served
  back by `dev_feed_get` / `dev_schedule_get`.
- **`play_sound` button and `selected_sound` number** added for camera feeders.
- **`disturbMode` restored for camera feeders.** Was removed claiming "not in
  firmware", but a proxy capture proves the cloud sends it.

## 2.0.1 — 2026-08-11

- Fixed a race condition when applying patchers to two devices at the same time:
  staged files are now isolated per device, so one download can no longer
  overwrite another's.
- `surplusControl` (Surplus Level select on feeders) now sends the paired
  `surplusStandard` value the firmware expects, and uses the real wire values
  (0/30/60/80) instead of option indices.
- The panel's select controls no longer revert to the first option on repaint
  when the entity has no explicit `option_values` (affected `flow_mode` on W7H
  fountains).
- Hall switches (~20 internal mechanism diagnostics on camera litters) are
  hidden from the panel's State card by default.

## 2.0.0 — 2026-08-10

Mostly a tidying release: no new device support, no new entities, and — apart
from the port fix below — nothing a working install will notice. The version is
2.0.0 because one option is gone and a lot of code moved.

### A remapped port is honoured now

Changing the add-on's Network mapping for `80/tcp` — to 8080, or anything else —
left every device unable to reach the server. `dev_serverinfo`'s `apiServers` is
the only thing that tells a device where we are, and it was built without a
port, so it always said 80. Reported by @strxno.

The Supervisor knows the real mapping, so the address handed out now carries the
host port, and the media bucket follows its own `9000/tcp` mapping the same way.
A port mapped to nothing is reported in the log instead of being advertised
anyway, and a remapped MQTT port is reported too — that one cannot be fixed from
here, because the firmware dials it from its own build and no response overrides
it.

An explicit `api_url` is still used exactly as written. Prefer host 80 anyway:
a device redirected by DNS dials 80 with nowhere to tell it otherwise.

### `bucket_endpoint` is an option

It existed only as a command-line flag, so nobody running the add-on could set
it. Set it when uploads have to go through a different hostname, port or
TLS-terminating proxy than the API.

### `mqtt_host` is gone

Nothing ever read that option. The broker address a device is given is derived
per request from the address it reached us on, which is what lets one instance
serve devices that see it under different addresses. Setting `mqtt_host`
validated, did nothing, and looked like a broken add-on.

Updating is safe: a value left over in your options is ignored rather than
rejected — tested on an install that had one. Delete the line when you next
edit the options, or leave it.

### Fixes

- The panel's capture listing and device-log listing read every file on the
  event loop that also serves the devices. A large capture could stall the HTTP
  server and the MQTT bridge.
- A backend error rendered in the panel as "No devices connected yet", which
  told people with working devices to go and provision one.
- The panel's WebSocket retried every two seconds forever; it now backs off, and
  survives a malformed frame.
- Nine panel endpoints answered 500 instead of 400 to a malformed request body.
- Proxy mode could leak the sockets of a replaced upstream session.
- A failed video thumbnail answered 500, which the Timeline has no fallback for,
  so the tile broke instead of showing the poster still.
- A `number` entity that declares no bounds is no longer given 0..100 and told
  to refuse anything outside it.
- An optimistic write to a BLE accessory honours the section its field lives in.
- Face photos and the go2rtc config are written off the event loop.

### Installing no longer means building

The add-on pulls a published image now — one multi-architecture package on
GHCR — instead of compiling everything on your own machine. On a Raspberry Pi
that turns a first install from many minutes of building `cryptography` into a
download. `docker-compose.yml` uses the same image, and its two site-specific
values moved to a `.env` file so they cannot be committed by accident.

### Underneath

Eleven files that had grown past what anyone reads in one sitting were split
along seams they already documented — the BLE protocol, event ingestion,
redaction, the middlewares, startup, the device payloads, the BLE relay, the
command sink, the panel. The device layer no longer imports Home Assistant's.
There is an `ARCHITECTURE.md` now, which is what the README and CONTRIBUTING
point a human at instead of the file written for AI agents.

## 1.10.0 — 2026-08-09

Two settings maps arrived, built from captures of PetKit's own app talking to a
**Purobot Ultra** and an **EverSweet Ultra AI**, and a live Purobot Max Pro 2
was watched being configured through proxy mode. Between them they settle a
handful of things this add-on had been getting wrong, and unlock about twenty
settings it was already storing with no way to reach them.

### Avoid Repeat Interval was 60x too short

The field is named `autoIntervalMin` and carries **seconds**. Picking "5min"
asked the box for a five-second interval, and "2h" for two minutes. PetKit's app
was captured writing 300 at the UI's five-minute minimum and 7200 at its
two-hour maximum, which is the whole range.

The capture is from one model. The same field exists in the older ESP32 boxes
and nobody has read its unit there, so applying this reading to them is a
decision, not an established fact — but a field with one name having one meaning
is the better bet than leaving every camera box with an interval it never asked
for. If your box holds a value from the old list it will show as a bare number
until you pick one.

### Reset N50 could pack the waste on a Purobot Ultra

That button sends `start_action: 8`. On a Purobot Ultra, a single tap of the
app's **Pack** action sends the same value — and that model has no N50 cartridge
to reset in the first place. So the button is gone there, and the code it sent is
now behind **Pack Waste**, named for what it does. **Open Sealed Door** joins it,
also that model's own hardware.

Nothing changes on the other boxes. Their Reset N50 keeps behaving exactly as
before, which on the evidence is: no reply at all.

### The litter type is no longer invented

`sandType` was seeded as `0`, and there is no `0` — the app's picker gives 1
clay/ore, 2 tofu, 3 mixed. That seed was served back to the device as the litter
it is filled with. It now stays unset until the box says otherwise, and the
panel shows the name instead of the number.

### Settings that existed with nothing to press

Every litter box gains **OK Button Under Child Lock**. A camera one also gains
**Pet Detection**, **Wander Detection**, **Toileting Detection**, **Voice
Prompt** and **Quiet Voice Prompts** — five settings this add-on has been storing
and serving to the device since the beginning, with no control for any of them —
plus **Light**, **Power Off** and **Power On**.

An EverSweet Ultra AI gains seventeen. It had a camera entity and no way to
switch the camera off: **Camera**, **Video/Photo Upload**, **Microphone**,
**Microphone Indicator Light**, **Night Vision**, **Timestamp Display**, **Pet
Tracking**, **Voice Prompt**, **Quiet Voice Prompts**, **Volume** and **Voice
Language**, along with the same two power buttons.

Its two water-treatment schedules are settable now as well — **Drain & Refill
Cycle** and **Drain & Flush Cycle** in days, with **Drain & Refill Time** and
**Drain & Flush Time** as real clocks. These four were deliberately withheld
before: their names were known from the firmware but not their encoding, and a
number entity needs a range. The capture gives it.

Power is a service of its own, not a settings write. Two fountain buttons were
removed a release ago for writing `power` as a setting to a field nothing reads;
this is the thing they were reaching for.

### Schedules are editable, and were never really being served

The panel gets a real schedule editor: clocks and weekday buttons instead of raw
JSON. It covers the screen period, both do-not-disturb periods, the camera's
shooting period, the litter box's scheduled cleaning and deodorizing times, and
a feeder's meals — time, weekdays and a portion per hopper. The raw JSON is
still there under a disclosure, and it is still the only way to edit a shape the
editor does not know.

The feeding schedule's shape came out of the firmware rather than off the wire:
`it` was an empty list in every capture, so `pk_schmg_parse_schedule` in a D4SH
`ctrl` is what names the fields. One consequence worth knowing — the unit of a
meal's time is **inferred**. Minutes since midnight is what every other schedule
on these devices uses, but nobody has watched a feeder receive one, so if your
meals land at the wrong hour that is the reason, and saying so is the fastest
way to get it fixed.

A fountain gets no Schedules card yet. Its firmware reads five of these and the
app writes them, but the reply that serves them back is still open work, and
storing a schedule this add-on cannot answer with would be the confusing half of
the feature.

It also fixes something that was invisible: **`dev_multi_config` read nothing.**
It looked like it resolved a stored value against a default, and did not — every
device was handed the same hardcoded screen period and quiet hours on every
poll, whatever it had been set to. In proxy mode that meant a period set in
PetKit's app was undone by our next reply. What the editor saves is now what the
device is answered with.

Saving also pushes the change rather than waiting for the device to ask, the way
the real cloud does. Two shapes travel that path and they are not the same one:
a period is a JSON string that wraps its own key again, and the cleaning
schedule is a plain JSON string of its array.

**Nothing schedules itself for you any more.** A litter box that had never been
given a cleaning schedule was answered with three entries — 09:45, 13:45 and
18:45, every day — so this add-on was running your box on a timetable you never
chose and could not see anywhere. It also handed out quiet hours nobody picked:
00:40–08:40 for cleaning and 22:00–06:00 for voice prompts.

An unset cleaning schedule is now empty, the way an unset feeding schedule
already was. If you had Periodic Cleaning switched on and never set the times
yourself, set them now — the editor is right there.

The periods now default to **all day**: screen period, shooting period, voice
undisturbed period, detection period. All day means "always", so it restricts
nothing and decides nothing for you. Cleaning Do Not Disturb is the one
exception and starts empty, because there "all day" would mean the box never
cleans on its own.

A word on the litter box's schedule: **one array holds both jobs**, cleaning and
deodorizing, told apart by a `type` field that every source — this one included
— had recorded as "always 0, meaning unknown". The editor shows them as two
sections and saves the whole array, so editing one cannot delete the other. An
entry with a `type` nobody has seen is shown under "Other" and left alone.

A fountain has five of these schedules and gets none of them yet: the firmware
reads them and PetKit's app writes them, but the reply that serves them back is
still open work. Storing a schedule this add-on cannot answer with would be the
confusing half of the feature.

### Under the hood

Home Assistant `time` entities are supported, for any setting that is one point
in the day. The device stores seconds since midnight; HA and the panel both show
a clock.

The protocol tables record what these captures settled: the litter box's
`start_action` values as the app's own buttons send them, the litter enum, the
three time units that live side by side in one settings block, and weekday
numbering — which starts at **Sunday**, confirmed on two models. That last one
has never mattered, because every schedule shipped here repeats on all seven
days, and it is exactly how it would have gone wrong the first time somebody
picked one day.

## 1.9.0 — 2026-08-08

### A YumShare Dual-Hopper is asked for food in the field it actually reads

Pressing Feed on a D4SH ran a feed cycle and dispensed nothing. The command was
well formed and asked for nothing: its firmware compares its own model string
against `D4SH` and, in that branch, reads only `amount1` and `amount2` — one per
hopper. The plain `amount` we sent is not looked at on that model, so both
hoppers were asked for zero and the device reported exactly that.

Two per-hopper controls come with it. **Hopper 1 Portions** and **Hopper 2
Portions** set how much each dispenses, defaulting to 1 and 1 because that is
what PetKit's own app sends for its default manual feed. **Feed Hopper 1** and
**Feed Hopper 2** dispense from one and not the other, which is what the app
does by asking the other hopper for zero rather than by calling anything
different. Plain **Feed** still uses both.

Single-hopper feeders are deliberately untouched. A D4H divides `amount` by a
constant held in its own configuration before using it, so the 10 that path has
always sent is not ten portions and is not ours to reinterpret. Nothing in the
report speaks to it — the reporter has a Dual-Hopper, which never reads that
field.

**Cancel Manual Feed** now uses `feed_realtime_cancel`, the service the firmware
has for it, naming the feed being cancelled. It used to send a zero `amount`,
which on a Dual-Hopper lands in a field the device does not read. The ESP32
feeders keep the old form: that service was read out of the embedded-Linux
binary, and the D4, D3 and Feeder Mini run different firmware.

### The Dual-Hopper's state stops disappearing on its own main channel

A D4SH publishes its state over MQTT as `property/post`, and that path went
through a normaliser carrying not one feeder field — so every hopper level, the
bowl reading and the feeding flags were dropped there while working fine inside
events. Both transports now read the same report the same way, which a test
asserts directly against the payload from the report.

Device Status no longer shows a made-up `0`. The device sends no work state at
all, and the parser was defaulting one, so the entity displayed a value that had
never been reported. The fault block reaches both paths too; it was wired into
one.

New on both D4H and D4SH, and each named for how well it is understood:

- **Hopper 1** / **Hopper 2** — has food or empty. Not a percentage: the owner
  saw 2 and 0 and never anything between them, so an unexpected value shows as
  the raw number rather than being rounded into a story.
- **Bowl Surplus** — leftover food, with no unit, because nobody can yet say
  whether its one observed reading was grams, a percentage or a count. `-1` is
  the firmware's "not measured", which it sets as a feed begins.
- **DC Voltage**, four hall switches, three infrared readings and `door`, all
  diagnostic. The last four carry the device's own words: `door` read the same
  value through the lid being opened, both hoppers pulled and the battery cover
  removed, so whatever it is, it is not the lid — and calling it one would have
  been a confident lie about somebody's hardware.

Four controls are withdrawn from these two models: Food Low, Food in Bowl, Food
Bowl Level and Battery Installed. Each reads a field this hardware does not send
— it says `food1`/`food2` where they expect `food`, and `batV` where they expect
`batteryPower` — so all four could only ever read unknown. The backup-battery
voltage is deliberately not published either: it reads 0 with no batteries
fitted, which looks like a flat battery rather than an absent one.

Reported by @dscarr10, who also captured the official app driving the feeder
through proxy mode. That capture is what made the fix a reading rather than a
guess.

## 1.8.2 — 2026-08-08

### The empty `dev_ble_device` answer goes back to omitting `list`

1.8.1 started sending `list: []` to a device with no accessories paired, on the
grounds that PetKit's own cloud does. Owners have since reported that empty
array crashing devices, so it is out again: with nothing paired the reply
carries `nextTick` and no `list` key, which is the shape that ran for months
without the complaint.

The argument 1.8.1 made was from analogy — the cloud sends the empty array 234
times in one capture, therefore it cannot be what breaks a device — and the
field beat it. What is still not known is which models are affected or why the
cloud gets away with it; that needs a capture from someone it happens to, and
until then the code says so rather than claiming more.

`nextTick` stays in both shapes and is not implicated in any of this. It is the
half of 1.8.1 that fixed something real: without it, a parent with nothing
paired was told neither what to scan for nor when to ask again.

Both transports changed together. The HTTP handler and the MQTT `data_get`
answer the same question and have drifted apart once already.

## 1.8.1 — 2026-08-08

### `dev_ble_device` now answers exactly as PetKit does

Comparing our reply against a capture of the real cloud (issue #6) turned up two
differences.

With nothing paired we sent `{"result": {}}`, where PetKit sends
`{"result": {"list": [], "nextTick": 3600}}`. Omitting `list` rested on one
firmware log line — an empty array producing `ERR:...parse item NULL`, read as a
null dereference that aborts the boot chain — and that reading did not survive
its own evidence: the same capture has PetKit answering the empty array 234
times in one session, so every unaccessorised device receives it routinely.

It was not free, either. The omission took `nextTick` with it, so a device with
nothing paired was told neither what to scan for nor when to ask again. Nobody
decided that half; it fell out of a shared empty-response helper.

The accessory MAC is canonicalised where it is stored rather than by each
caller. Both existing callers did normalise, so nothing was broken — but a third
that forgot would have put `aa:bb:cc:dd:ee:01` on the wire where the cloud sends
`aabbccddee01`, and every lookup here would have carried on working, because
those compare canonical forms. The device would have been the only thing to
notice, by never finding the accessory.

## 1.8.0 — 2026-08-08

### The API is published on port 80 now — read this before updating

An ESP32 model provisioned with any other port gets onto Wi-Fi and then fails at
the server connection, tested repeatedly across two devices by @strxno, and one
pointed here by a DNS redirect dials 80 with nowhere to tell it otherwise. The
add-on publishes on host 80, and the compose example maps `80:8080`. Inside the
container nothing moved.

The auto-detected `api_url` moves with it, which is the same change and not a
second one: left at `:8080` the add-on would advertise a port it no longer
publishes. An explicit `api_url` is still honoured verbatim, and Home Assistant
keeps a port override set in Network across updates.

**Anyone already provisioned on `:8080` has that port burnt into the device**
and needs re-provisioning, or the old mapping set back by hand in Network.

### Ask PetKit for what only PetKit knows

An accessory's pairing secret and a pet's reference photos exist in one place —
the account — and until now the only way to get either was to read it off the
hardware or retype it from the app. Two buttons now ask for them directly: one
on a device's BLE card, one on the AI / Pets tab.

The request is signed the way the firmware signs its own. T4 firmware 1.652
carries both halves of it as adjacent format strings —
`id%snonce%stimestamp%utype%s%s` hashed, `id=%s&nonce=%s&timestamp=%u&type=%s&sign=%s`
sent — so this reproduces the header rather than guessing at it. Confirmed
against the real cloud: `dev_ble_device` and `dev_device_info` both answered
200 to a request built here, eleven seconds before the device asked for the
same two and got the same answer.

Two details that are easy to get wrong. The `type` field is hashed, so `T5` and
`t5` are different requests — the device's own spelling is recorded from live
traffic and persisted, because a restart that lost it would sign the next fetch
with our lowercase codename and be refused. And PetKit answers `704` when the
credential is one we issued rather than one it knows, which the panel says in
those words instead of reporting a generic failure.

Nothing is fetched on a schedule. This runs when a button is pressed and at no
other time — the add-on does not poll PetKit, and must not start.

A pet imports under a new local id with PetKit's own id bound to it as an
alias, because that foreign id is exactly what a box still matching against
cloud-cached faces reports back. Binding it names the history already recorded
under it, retroactively. Names are in no payload a device receives, so an
imported pet arrives as `PetKit pet <id>` — click the name on its card to
change it, which was not possible here before at all.

### A litter box can finally be told about its spray

`dev_k3_device_info` had no route: it fell through to the catch-all, and a T4
asking what its Pura Air is configured as was answered `{}`, forever
(issue #17). The reply now carries what we actually know, in the field set T4
firmware 1.652 parses by name — identity, whatever readings the spray has
reported, and its settings when a real value exists. Every key in that parser is
looked up individually, so an absent one is skipped rather than read as zero;
that is what makes it safe to send only what we have and invent nothing.

`settings.k3Config` used to go out as an empty object against six keys the
firmware reads by name. It now carries them.

`relateT4` is deliberately still absent. Its name and its `%d` admit two
readings — the parent's id, or a 0/1 flag — and the wrong one lands on the
firmware's `diff ID` path.

The reboot half of that issue is not addressed.

### Bluetooth accessories, from @strxno's hardware

- **A write opens the session it needs.** Published on its own,
  `thing/service/ble` is accepted by the parent, forwarded, and does nothing.
  The same 221 frame is acknowledged with a session held open and ignored in
  silence without one, and from here those two look identical.
- **The session is held open, and hung up only on a real reading.**
  `thing/service/connect` carries a `time` field; without it the parent lets the
  radio go before the accessory has said anything worth having. We also hung up
  on ANY reply, including the short 251/252 that precedes a CTW3's run-info
  pass — which is most of why a CTW3 was so hard to get anything out of.
- **A parent is told its accessory list changed.** `dev_ble_device` answers
  `nextTick: 3600` and the parent honours it, so pairing left the device that is
  meant to scan knowing nothing for up to an hour — while the poll pushed
  `connect` for a MAC it had never been told about, which fails looking exactly
  like a wrong scan type. `ble_relay_update` is the trigger, confirmed on a T5.
- **A CTW3 answers cmd 211 over the relay**, though it is silent to a direct
  GATT client on firmware 111. The reply also settles the byte order by
  write-and-read-back: "light off" moved byte 6 and left byte 7 alone.
- **A fountain that is switched off no longer forgets its mode.** A W4, W5, W4X
  or CTW2 reports mode 0 while powered off; that is not a mode, and storing it
  cost the last real one — so switching the fountain back on silently made a
  smart-mode fountain a normal-mode one.

### Provisioning

- The Bluetooth characteristics are found by property when the expected UUIDs
  are not there, and a failure now lists what the device actually exposed
  instead of giving up quietly.
- BLUFI `0x0f` and `0x12` are decoded again, read-only. Neither decides
  anything — the PetKit keys inside custom data drive provisioning — but a
  device that fails at the BLUFI layer says so in one of them, and undecoded
  that arrived as "ignored ESP32 packet subtype 0x12".

### Also

- The patchers work on Axera ARM boards, which keep the writable boot override
  under `/opt` where Ingenic boards use `/system`. Probed rather than decided by
  codename, because D4SH and D4H ship as both.
- The bucket endpoint can be set for a standalone Docker run.

## 1.7.0 — 2026-08-03

### An ESP32 device was being provisioned onto PetKit's cloud

A T4 came up online, visible in PetKit's own app, having never once called this
add-on. The Bluetooth log said it had connected to a server, and it had — just
not to ours.

BLUFI can provision Wi-Fi by itself: set the mode, hand it the SSID and the
password, tell it to connect. This did all of that, and sent PetKit's own
document with the server address alongside as an extra. So the ESP's BLUFI
layer joined the network on its own, the firmware's handler for that document
never ran, and the device carried on with the server list it already had.

PetKit's app sends no native Wi-Fi frames at all — `PetkitBLEManager` has one
outbound call in it — and the SSID and password travel inside the same JSON as
everything else, so the firmware does the joining itself *after* reading where
to phone home. This now does the same. A device that ignores the document will
fail to join, which is loud, rather than join somebody else, which is silent.

### The log-upload guard also stops the device reporting what it recorded

`dev_upload_file_info_v2` is how a device says what it just uploaded: the file
id, the module type, the AES IV, the event it belongs to and the
pet/clean/toilet flags. Proxied, that is a running account of what happened in
somebody's home — every visit, every clip, timestamped — sent to PetKit by a
device its owner has taken off PetKit. The media itself never gets there, which
makes the metadata the whole of what they would learn.

Redaction could not cover it: that rewrites response bodies, and by the time
there is a body the request has been delivered. So it is withheld on the way
out, the same way a log upload naming our own bucket already was. Switching the
guard off proxies it again — it is a guard, not a permanent exemption.

### PetKit's own app, as a source

The provisioning payload and the CTW3 settings block were both settled by
decompiling PetKit's Android app (13.8.1, `com.petkit.android`). Nothing from it
is in this repo — only what the protocol is.

- **`locale` is a time ZONE name, not a language.** The app fills it with
  `TimeZone.getDefault().getID()` — `Europe/Amsterdam`, `America/New_York` —
  right beside the numeric offset, and a captured D4 signup body echoes exactly
  that back. This sent `navigator.language`, so devices were being told their
  zone was called "en-US".
- **The CTW3 settings block is twelve bytes, in the order this already read
  it.** 1.6.0 reordered three of them for the write, following a third-party
  capture, and that was wrong: the app writes lamp switch, brightness,
  do-not-disturb, child lock at bytes 6-9, exactly as the status tail reads.
  Bytes 10 and 11 are two more switches nobody had named — the smart and
  battery inductive sensors — and they are decoded and restated now.
- **Filter reset carries no payload.** The app sends cmd 222 empty; 1.6.1 sent
  a single zero byte.
- **Choosing a flow mode sends power on and pump un-paused**, both fixed at 1.
  1.6.0 derived the pause byte from the mode, which the app does not do.
- **The join report now names what went wrong.** Its ten states come from the
  app's own per-state log lines, including the four failures it had none of:
  a wrong Wi-Fi password used to render as "state 3" and the panel kept polling
  for another twenty-four seconds. State **9 is "connecting to MQTT"** and
  **10 is "online"** — an ESP32 device cannot reach either against this add-on,
  because the TLS bypass it would need has no ESP32 patcher, so it settles on
  the HTTP heartbeat. Stopping at 7 is therefore not a failure.
- The transport split is confirmed to be by model, and it is the same list this
  already calls next-gen: T5, D4SH, D4H, T6, T7, T7 Lite and W7H take PetKit's
  own GATT profile, everything else BLUFI. The JSON document is identical on
  both; only the framing differs.

### Camera feeders had no stream URL, and no patchers either

A D4H or D4SH reports its LAN address in the same free-form string a litter box
does, and the feeder parser was the one that dropped it. Nothing downstream said
so: go2rtc quietly skips a device with no address, and the Patchers tab reports
the whole device as unsupported. Found and fixed by **@nklmilojevic** (PR #12),
confirmed on a D4H.

The extraction now lives in one place for all three parsers — the litter one
knew only the flat key, the MQTT one only the string — and it checks what it
matched. The old pattern accepted `....` as readily as an address, and this
value becomes a go2rtc source and an SSH target.

### A device is told back the timezone it reported

Its signup body carries `timezone` and `locale`, and both were read off the wire
and dropped, so the reply came back with the SERVER's offset and an empty
locale. A device that had just said it was at -4.0 was answered 2.0. The device
is the authority on where it is; a manual override still wins over both.

### The panel can tell you when you are looking at an old copy of it

A 1.6.1 install reported a Bluetooth failure whose log was, word for word, the
output from before the fix — the browser was still running the previous
`app.js`. The server was fine. Nothing anywhere said so, and the only way to
establish it was to compare log strings against the source.

Assets are already versioned by a hash of their content, which defeats a stale
asset. It cannot defeat a stale *document*: `?v=<hash>` only reaches the
browser inside the markup that carries it, so a cached index asks for the old
asset URL and every cache answers it correctly. The index now says `no-cache`
itself instead of getting the header from a match on its path — behind Home
Assistant Ingress the panel is mounted under an opaque prefix, and how that
request arrives was never ours to decide.

And the page now knows which build it is. The stamp travels in the document,
`/api/info` answers the same value live, and a difference between them can only
mean the page is old — so the panel says so, at the top, with a reload button.
The version in **Setup** does not answer this: it comes from the server, so a
fresh server behind a stale page reports the new number while running the old
code.

## 1.6.1 — 2026-08-03

Bluetooth provisioning, from three device reports. The short version: the panel
was writing in one dialect and reading in another, and where a device did
answer, the answer was thrown away.

### Which way a device wants to be written to is now asked

A Purobot Ultra takes a framed envelope — `FA FC FD 46 | type | seq | len |
json | crc16 | FB` — and ignores anything else. A YumShare Solo takes bare JSON
with response, ignores a framed write in silence, and drops the first write
after a connect. The two are mirror images, so no single hardcoded direction
could ever serve both.

The panel now probes with `key 110`, uses whichever framing the device answered
in, and runs the real handshake: credentials, then the device's own join
report, rather than a fixed wait. Contributed by **@nklmilojevic**, verified on
a D4H (fw 867), on top of the protocol **@strxno** reverse-engineered and
verified on a T6 (fw 951). Both models are confirmed working.

Both join states count as joined: the T6 goes `0 → 1 → 6 → 10` and never
reports 7, the D4H stops at 7. Waiting on either alone hangs on half the
models.

### A Pura Max was answering, and was told it had not

A T4 replied three times, the log printed `type 1 subtype 0x13`, and the panel
said the device never answered. `0x13` is BLUFI's **custom data** — the channel
PetKit's own document travels on, the one this sends the credentials over — and
the only replies being read were BLUFI's Wi-Fi report and its error report. The
device was answering in PetKit's protocol, inside BLUFI, and it was discarded.

Custom data is decoded now, reassembled across fragments (BLUFI splits anything
past its chunk size, and a fragment is not JSON on its own), and provisioning
counts as done on either protocol's confirmation.

### Smaller

- A device that accepts the credentials but does not report joining within 25
  seconds says so, with its last known state. It used to return success with
  nothing in the log at all — the one habit 1.5.0 set out to break.
- The join report is logged as it changes, so the log shows progress rather
  than one line at the end.
- Notifications are read from their own offset within the underlying buffer.
  Reading the buffer whole works in Chrome today and is the kind of thing that
  stops working on one platform with nothing to see.
- The credentials now carry `hide`, `ipServers` and the timezone as the
  one-decimal string, matching the only payload confirmed to provision a
  device.
- The provisioning decoders have real tests: the frames from all three reports,
  run through the actual panel code in node. Skipped where node is absent.

### Two feeder details, from a capture of the real cloud

Both from **@cipheredsyntax** (PR #10), taken off PetKit's own servers talking
to a Fresh Element Solo:

- **A feed's id carries its number twice** — `r_20260802_882_882-1` — where
  this sent it once. That shape came from localkit's reimplementation; two
  independent captures agreeing outrank it.
- **A settings write carries no `type` key.** It is inert — the firmware reads
  `payload` and nothing else — but a body shaped like the cloud's is one a
  capture of ours can be compared against.

The rest of that report — a feeder sending its identity in the POST body, and
the heartbeat's `msgType` numbers being wrong — was found independently and
fixed in 1.5.0 and earlier, in both cases more broadly than the report needed:
identity is resolved from the body for every endpoint rather than signup alone,
and the `msgType` values come from the firmware's own three-way branch, read in
two different binaries, rather than being special-cased per family.

### Still true, and worth saying plainly

The bytes this sends an Ingenic device were never the regression. They are
identical in 1.4.0 and 1.6.0 — same document, same chunking, same write. What
1.4.0 did was print "provisioned" without looking at the reply; 1.5.0 started
telling the truth and 1.5.1 fixed the model detection it had broken. This
release is the first that reads what the device says back.

## 1.6.0 — 2026-08-02

### Every Bluetooth write this has ever sent was malformed

The frame that carries a command to a fountain announces its own payload length
in two bytes, low half first. This wrote one. Everything after it was therefore
shifted by one, and the accessory read the first byte of the payload as the
high half of the length: a mode change claimed four bytes and delivered three
and a trailer, and a settings change claimed **780 bytes** and was still being
waited for when the session closed.

Both fail in silence — there is no error, the frame is simply never acted on —
which is why "a settings write cannot be verified" has been in the notes here
since the write path was added. It could not have worked.

Found by comparing this against [aavdberg/ha-petkit](https://github.com/aavdberg/ha-petkit),
which reaches these fountains straight from Home Assistant rather than through
a relay and has therefore had every one of its command layouts exercised on
real hardware. Three sources agree on the eight-byte header, including this
project's own frame *reader*, which has always expected one — so the encoder
and the decoder here were written to two different specifications and nobody
noticed, because nothing ever round-tripped one.

With the length fixed, the mode frame drops the leading zero it used to carry
— that zero was the missing length byte, compensated for in the wrong place —
and becomes byte-identical to the one ha-petkit sends.

### Fountain controls that did the wrong thing when they worked at all

- **Picking a flow mode switched the pump off.** The frame carries power, pause
  and mode together, so it was rebuilt from the last reading — and a CTW3
  reports power 0 in the sleep half of its smart cycle. Choosing a mode now
  states that the pump should run, because that is what choosing a mode means.
- **A mode of 0 was believed.** Same sleep phase, same report: it is not a mode
  and it is not "off", it is the fountain resting between runs. Latched, it
  turned the next touch of the power switch into "off, in no mode". It is
  discarded now and the last real mode stands.
- **Switching a fountain off left it marked as pumping.** The pause byte is
  forced to 0 when the power is.
- **The settings block was written with invented bytes.** Two constants at the
  front were the smart-cycle times one captured frame happened to carry,
  mistaken for structure, and one byte near the end was a value that frame does
  not have. It is rebuilt from the reading now, and the smart-cycle times are
  yours to set.
- **The flow modes are called Normal and Smart**, as PetKit's app calls them
  and as both reference projects do. They were "continuous" and "intermittent"
  — accurate about the pump, matching nothing you can compare against. An
  automation that names the old labels needs updating.

### Where the two projects disagree, the code says so

Three bytes of the CTW3 settings block are read one way here and the other way
by ha-petkit. This reads them from a real status frame, where they are coherent
as light / brightness / do-not-disturb; ha-petkit reads the same positions in a
capture of PetKit's own app writing, where they are do-not-disturb / light /
brightness. Neither observed the other's direction, and nothing says a status
echo is byte-identical to a write.

So the status is decoded the way the status frame reads, the write goes out the
way the write capture reads, and the disagreement is written down where the
bytes are rather than resolved by preference. If a CTW3 owner finds the Light
switch turning do-not-disturb on, that is the note to read.

### Fountains gained what they were already reporting

- **CTW3**: child lock, both smart-cycle times, a module status byte, and a
  power source that says "mains" instead of `2`. An older firmware that sends a
  26-byte status is read rather than dropped — this demanded 30.
- **W4 / W5 / CTW2**: today's pump runtime and both smart-cycle times, and the
  settings frame is decoded at all now, which is where the light, its
  brightness, both schedules and the child lock live.
- **A filter reset** on both families, as a button. It carries nothing built
  from state, so it is the one write that works on an accessory that has never
  reported.
- A status frame too short to be one is dropped whole on the W5 family too. It
  used to emit every field whose offset happened to fit, which turned a
  one-byte acknowledgement into a confident "the pump is on".

### Controls for the W4 / W5 / CTW2 fountains — untested

They had five entities, all read-only. They now have power, mode, light,
brightness, do-not-disturb, child lock, both smart-cycle times and the filter
reset. **Nobody involved owns one.** The layouts come from two independent
sources that agree, and that is all that can be said for them; a settings write
is refused until the fountain has sent a settings frame, rather than guessed
from defaults that would erase both schedules.

`w5_power` became a switch, having been a read-only sensor. If you are running
one, the old `binary_sensor` entity is orphaned and the switch replaces it.

## 1.5.1 — 2026-08-02

### Bluetooth provisioning worked again, on the models 1.5.0 had not broken

1.5.0 taught the Provision tab to recognise BLUFI as well as PetKit's own
protocol, and picked between them by asking the device to list its Bluetooth
services. That list turns out not to be the same thing as what a device hands
over when asked for a service by name: a YumShare Dual-Hopper that opens
PetKit's `0xAAA0` on request does not appear in it, so the tab decided it did
not recognise the feeder and stopped.

Reported by an owner who paired the same device from the hosted provisioning
page minutes later — that page still runs the older code, which asked by name.
Two pages, one device, one difference; without that comparison this would have
read as a broken feeder.

Both services are asked for by name now. The listing survives in one place,
describing a device that answered to neither, where being incomplete costs
nothing.

### Two places pointed at tabs that do not exist

The provisioning log told anyone who selected a Bluetooth-only accessory to
pair it under "Setup", and the capture toggle was described as living under
"Setup → Live settings". Neither exists: accessories pair from **Devices**, on
the panel of the litter box or feeder that relays for them, and the capture
toggle is under **Setup → Settings**.

## 1.5.0 — 2026-08-02

### Some models could never register at all

A Feeder D4 sends its id and serial in the body of its signup request, not in
the header every other model uses. Nothing here looked at the body, so the
device was answered "missing device id" and stopped there — and the endpoint
that hands out MQTT credentials answered **200 with nothing in it**, which is
worse, because there is no error to see. The device simply never appeared.

The body is a third place identity can come from now, after the header and the
query string. This is one report's evidence (thank you), but it is not one
model's bug: about eighteen endpoints resolve a device the same way.

### Bluetooth provisioning on the ESP32 models

The Provision tab spoke one protocol and assumed every PetKit device spoke it.
The ESP32 models do not: a D4 and a T4 carry **BLUFI**, Espressif's own
provisioning profile, and expose nothing at the address the panel was looking
for. Selecting one produced a Bluetooth error about a missing GATT service.

The panel now asks the device which of the two it speaks and uses that one. The
Wi-Fi credentials, the server address and the timezone are the same either way.

Two related things that were wrong for every model:

- **"Provisioned" meant "the write returned".** It was printed whether or not
  the device had understood a word, and the Bluetooth link was cut a second and
  a half later — before a slower reply could arrive. It now means the device
  answered, and says plainly when it did not.
- **The device chooser offered accessories.** A W5, a CTW3 or a Pura Air would
  appear in the list and then fail with a raw error. They have no Wi-Fi to
  configure; the panel says so and points at where they are actually paired.

Feeder Mini and its generation have no Bluetooth setup at all — those still
need a DNS redirect, and the panel names that too instead of failing silently.

### The EverSweet Max Cordless

A CTW3 owner mapped their fountain's whole protocol and sent it in, which is
the only reason any of this exists. It is supported now: tanks, pump, battery,
filter, drinking detection, faults — and its controls, which is the first time
any Bluetooth accessory here could be *set* rather than only read.

Power, working, flow mode, light, brightness, do-not-disturb and both timers.
Two caveats worth knowing: a setting can only be changed once the fountain has
reported at least once, because the device takes its settings as one block and
the rest of it has to be read before one part of it can be written; and the
number the relay is told to scan for is **24** for this model, not the 14 that
1.4.0 guessed.

### A Bluetooth accessory is a device in the panel now

It was three cells in its parent's card: type, address, unpair. Everything the
last two releases added to it — the decoded readings, twenty-one entities, the
controls — existed only in Home Assistant.

It gets its own panel in **Devices**, next to the device that relays for it,
with its state and its controls. Not a copy of a device panel: there are no
HTTP or MQTT counters, no command queue, no patchers. An accessory has no
network of its own, so every one of those numbers would be a zero pretending to
be a reading. What it does have is a **BLE** badge instead of MQTT-or-heartbeat,
a line saying which device relays for it, and — at last — when it last said
anything, which was previously not recorded anywhere at all.

Its controls work from the panel too, not just from Home Assistant, and there
is a **Read now** button: an accessory speaks only when its parent is told to
open a Bluetooth session, which otherwise happens on a timer up to four minutes
away — no way to answer "did that pairing work" except to wait. Where the scan
type is still a guess, the panel now says so on the accessory itself rather
than keeping it in a field nobody reads.

### Accessories were never being asked to report

An accessory only speaks when its parent is told to open a Bluetooth session,
and the only thing that ever told it was the arrival of a status report from
the parent itself. A feeder does not send those. So a fountain relayed by a
feeder was polled zero times, for ever, while looking perfectly paired.

There is a timer now, which is what the real cloud uses. The session is also
closed once the reading is in, instead of being left open indefinitely, and a
Pura Air is no longer asked to open one at all — it is not reachable that way
and never was.

## 1.4.0 — 2026-08-02

### Commands sent over HTTP never arrived

A device that is not on MQTT gets its commands in the answer to its heartbeat
poll. Each one is tagged with a `msgType` telling the firmware what kind of
message it is — and four of the five numbers we used were not numbers the
firmware knows. It logs `error msgType` and discards the message: no reply, no
error, nothing this end can see. The queue drains, the add-on reports the
command delivered, and the device does nothing.

Only "start" was right, by coincidence. **Every settings change, every manual
feed and every connect sent to a device without an MQTT session was thrown
away.** They are the three real values now (0, 1 and 2), read out of two
different models' firmware to be sure it is not one device's quirk.

Two commands answered in the same poll also collided: the device drops a
message whose timestamp is not newer than the last one it ran, so only the
first of a batch was executed. They are spaced now.

This affects every model, not just the fountain.

### The EverSweet Ultra AI had its tanks the wrong way round

The tray and the waste tank were swapped. "Waste Tank Full" was the drinking
tray being full; "Drinking Tray Installed" was the waste tank being seated. An
owner reported this and was told the firmware said otherwise — it does not. The
field is filled by a function the firmware itself calls "get water tray full
state", which is as direct as evidence gets.

Four entities are renamed as a result. **The old ones will stop updating and can
be deleted from Home Assistant:** Waste Tank Full, Waste Tank Installed,
Drinking Tray Installed and Drinking Tray State. What replaces them is Tray
Full, Tray Installed, Waste Tank Installed and Waste Tank State. Transfer Pump
is now Refill Pump, which is what the firmware calls it.

### Flush, Refill and Water Change

The W7H can be told to run its water cycles. It had no buttons at all, because
the values its `start` service takes were unknown; they are now read out of the
firmware, which accepts exactly twenty of them and silently ignores everything
else. Flush and Water Change name themselves in the firmware. Refill is on the
accepted list but not named there, so if one of the three turns out to do
something else, it is that one.

Deep clean is deliberately absent: the cycle exists, but nothing ties it to an
accepted value, and it is the one that needs somebody standing there with a
kettle of boiling water.

### The fountain stops borrowing a litter box's vocabulary

A refill showed up in the timeline as "Odor removal", a flush as "Dumping". The
work-mode names are per device family and the fountain was being read out of
the litter table. It has its own now.

Five more faults are spelled out — clean and waste tank missing, clean tank low,
waste tank full, heater missing — and a fault that arrives as an event reads the
same as the same fault arriving in a status report, instead of showing the
device's abbreviation.

The W7H also gets its Work, Drinking and Error event entities, which are what
automations trigger on. Until now its events were published to entities it had
never announced, so nothing fired.

### Which fountains can actually connect

Only the EverSweet Ultra AI. The EverSweet, EverSweet 3 Pro, Solo 2 and Max
Cordless have no Wi-Fi at all — they pair over Bluetooth to a litter box or a
feeder, which relays for them. They were listed here as network devices because
PetKit's cloud describes them that way, and from the account side a relayed
fountain looks exactly like a connected one.

The add-on knows which ones those are now: a model with no radio that somehow
registers over the network is logged as the anomaly it is, rather than quietly
getting an entity list nothing can fill. The Pura Air spray is marked the same
way. Nothing changes for a fountain you already have paired through a box.

All four can now be paired, not just the W5 — they share one Bluetooth protocol,
so the same entities and the same frame decoding cover them. One number in that
pairing is a guess: what the relay is told to scan for. Only the W5's was read
off a real exchange, and the rest reuse it. If one of them stays silent, there
is a **Scan type** field under Advanced to try another value in; it is the kind
of thing only somebody holding the hardware can settle.

### Under the hood

The W7H is separated from the Bluetooth EverSweets throughout. It shares a
category with them because it is a fountain, but almost none of its protocol:
two tanks, a lift valve, ten hall switches, a camera and an NPU against a pump
and a filter. Every table read out of its firmware is now keyed on that model
alone, so a W4 or W5 can never be answered in a vocabulary describing hardware
it does not have.

## 1.3.0 — 2026-08-01

### The EverSweet Ultra AI can be patched

The MQTT TLS bypass and Local Storage patches now work on the W7H — the first
ARM device. Previously only the Ingenic MIPS models (T5, T6, T7, D4H, D4SH)
could be patched; the W7H showed "no arm variant yet" on every card.

The patchers detect the device's CPU from the binary they download rather than
from a static table, so a device the table has never heard of still gets the
right patch — and one the table has wrong (such as a hypothetical ARM
generation of an existing codename) is refused instead of silently mis-patched.
`DEVICE_CPU_ARCH` and the panel's architecture gate are removed; the binary
itself is the authority now.

ARM patch points were contributed by an external reverse engineer and verified
against the W7H 456 firmware image in the test suite. The MQTT stub is an
8-byte Thumb sequence that clears the TLS verification flags and returns
success, mirroring the 16-byte MIPS stub. The cloud patcher finds isCClassIP
by its unique ITE instruction tail and the five CONNECT_TO guards by the
CURLOPT constant they load, distinguishing them from two structurally similar
sites that must not be touched.

## 1.2.0 — 2026-08-01

### The YumShare Dual-Hopper can be patched again

Applying the MQTT patch to a D4SH failed outright with "Cannot find
mbedtls_x509_crt_verify_with_profile". The symbol was there and was found — the
patcher then threw the answer away, because it checked the first four
instructions against a copy taken from a Purobot Ultra. Those instructions carry
an offset that differs in every binary, so the check could only ever pass on the
one model it was recorded from. It now trusts the symbol, and the fallback for
a binary without one matches the *shape* of a function entry rather than one
build's bytes.

The Local Storage patch had the same fault in a quieter form: it expected an
instruction to be the last one in its function, which is where the compiler put
it on a Purobot and not on a Dual-Hopper. It now looks through the whole
function.

Both are fixed against real firmware rather than reasoning — T5, T6 and D4SH
images are now part of the test suite (`pytest --firmware`), so a patcher that
works on one model and not another fails here instead of on your device.

### The panel says which version is running

New row at the top of **Setup → Connection**. This exists because there was no
way to answer "did the update actually take effect?" — an owner updated, saw
the old entities, and neither of us could tell a stale build from a bug. The
number is now on screen.

### Fountain events

- **Refill done** (`add_water_over`) is a real event. A live EverSweet Ultra AI
  sends it a second after it starts drinking-detection; with no entry for it the
  timeline said `add_water_over (other)`.
- **Work started** was filed as something only litter boxes send. Fountains send
  it too.

### The AI detections moved

Pet, drink and vomit detection now sit on the **AI / Pets** tab beside the
recognition toggle, instead of among Controls with the heater and the flush
cycle. They decide whether a whole class of event is raised at all, which is
what the AI card is about. Vomit detection also gained a description.

Feeders with a camera but no on-device AI (D4H, D4SH) keep their pet-detection
switch where it was — they have no AI card for it to move to.

### Add-ons are called Apps now

Home Assistant renamed them in the interface, so the README, the documentation
and the panel say "app" where they mean the thing you installed. If your menu
still says *Settings → Add-ons*, you are on an older release and everything
works the same; the install steps note both. The word "add-on" survives
everywhere it is still correct — `ha addons`, the Supervisor API, this
repository's own layout.

## 1.1.0 — 2026-08-01

### The EverSweet Ultra AI (W7H) reports what it actually sends

Its entities were borrowed from PetKit's cloud API, which describes a different
generation of fountain. The ones that mattered read unknown forever while the
device was reporting a full mechanism nobody was looking at.

- **New:** waste and clean water tanks, drinking tray, waste lock, heater,
  circulation and transfer pumps, refill, flush and disinfect cycles, the lift
  valve, and the ten hall switches behind them. Plus Last Drink, Last Pet
  Detected and Reboot Reason.
- **Faults are spelled out.** "Tray full" rather than `taryF`.
- **Removed, because this hardware has none of it:** filter level, filter days,
  battery, low battery, replace filter, water lack, pet detected, drink times.
  Also the pause and resume buttons — `power` is not a command this firmware
  answers, so they wrote a field nothing reads and reported success. Delete them
  from Home Assistant if they linger.
- **Drink Times became Last Drink.** The device reports the *time* of the last
  drink, not a count, so the old sensor would have shown a ten-digit number.
- A W7H no longer reports a Device Status of 0. It never sends one, and 0 is a
  real mode — the same defaulting that once had an idle litter box calling
  itself busy.
- New switches for settings its firmware really accepts: drink and vomit
  detection, auto flush, auto water change, the three status lights, the WiFi
  light, and quiet modes for refill and water-level alerts.
- `drink_start` and `pet_discern` are recognised on fountains instead of being
  filed as unknown events, and a detection that recognised nobody is no longer
  recorded as a match on a pet.

### Everything else

- **Provisioning over HTTP said the wrong thing.** On a plain-HTTP page it
  reported that your browser could not provision — on Chrome, where the real
  and fixable problem was the page not being HTTPS. It now says so, the warning
  is coloured like a warning instead of a plain card, and the form is visibly
  switched off while it cannot be used.
- Litter boxes with a camera (T5/T6/T7) gained their six hall switches as
  diagnostics, which is how you tell where a stalled mechanism stopped.

## 1.0.1 — 2026-07-31

- The web panel adds itself to Home Assistant's sidebar on first start. It is
  not something an add-on can declare, and a fresh install hid the panel that is
  its whole interface. Done once — if you take it out of the sidebar, it stays
  out.
- Say that the MQTT broker needs configuring. With the Mosquitto add-on there is
  nothing to do; with any other broker you must set `ha_mqtt_host`, and skipping
  it produced no error at all — the device worked, the panel worked, and Home
  Assistant showed no entities. The add-on now warns.
- **Standalone only:** the device was told to upload its photos and video to
  `https://localhost:9000`, which on the device is the device. The address is
  derived from `api_url` now. Add-on installs were never affected — the
  Supervisor supplies a host address there.

## 1.0.0 — 2026-07-31

Initial release.

What it does, which models are actually verified, and the rough edges worth
knowing before you trust it are in the [README](../README.md) and
[DOCS.md](DOCS.md).
