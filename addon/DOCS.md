# PetKit Local

Local cloud replacement for PetKit devices, packaged as a Home Assistant app.
Your PetKit litter boxes, feeders, fountains and Bluetooth accessories talk to this app
instead of PetKit's servers; entities appear in Home Assistant via MQTT
discovery. Nothing leaves your network.

```
PetKit device ──HTTP───► petkit-local ──MQTT discovery──► Home Assistant
              ──MQTT──►  embedded broker ──bridge──►
              ──HTTPS─►  media bucket ──decrypt/remux──► /media/petkit
```

## Requirements

- A broker for the **MQTT** integration, so entities can be published. With the
  Mosquitto app the credentials come from the Supervisor and there is nothing
  to configure. With any other broker — one on your LAN, or in another
  container — **set `ha_mqtt_host` in Options**, plus `ha_mqtt_user` and
  `ha_mqtt_pass` if it needs them. Nothing warns you if you skip this: the
  device side works, the panel works, and Home Assistant simply never sees a
  single entity.
- A way to point the device at this app. BLE provisioning works on every
  model; ESP32 models can also be redirected by DNS. See the repository README.

## Installation

1. **Settings → Apps → Install app → ⋮ → Repositories**, add this repository's URL,
   then install **PetKit Local**. (Older Home Assistant releases call the same
   place *Settings → Add-ons → Add-on Store*.)
2. Configure the options (below) and start it.
3. Redirect your device to the app, then power-cycle it. It registers itself
   and its entities show up in Home Assistant.

## Options

| Option | Default | Description |
|--------|---------|-------------|
| `api_url` | *(empty = auto)* | URL the device is told to use for the HTTP API. **Leave empty to auto-detect the HA host's LAN IP** (recommended — embedded PetKit devices can't resolve mDNS `.local`). Override only for a specific address/port, e.g. `http://<ha-host-ip>/6/`. A `.local` value is ignored in favour of the detected IP. |
| `bucket_endpoint` | *(empty)* | Where devices upload photos and video. Empty derives it from the host IP and the port `9000/tcp` is published on. Set it when uploads must go through a different hostname, port or TLS-terminating proxy. |
| `ha_mqtt_host` | *(empty)* | **Your Home Assistant MQTT broker**, so entities can be published to HA. Auto-detected if you run the Mosquitto app; otherwise set it (an external broker's hostname or IP). Leave empty to skip HA publishing entirely — the device side still works. |
| `ha_mqtt_port` | `1883` | HA MQTT broker port. |
| `ha_mqtt_user` / `ha_mqtt_pass` | *(empty)* | HA MQTT broker credentials (leave empty for anonymous). |
| `log_level` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. |
| `offline_timeout` | `180` | Seconds without contact before a device is shown unavailable. |
| `mqtt_tls` | `true` | Add a TLS listener to the device-facing broker (self-signed, generated on first start). |
| `mqtt_tls_port` | `443` | Port for that listener. Change this and the matching port mapping together if your device dials a different one. |
| `mqtt_strict_auth` | `false` | Enforce the Aliyun HMAC signature. Off by default so a signature nuance cannot lock a device out. |
| `esp32_aliyun_mqtt_host` | `false` | Tell ESP32 devices (T3, T4, the ESP32 feeders) to reach the broker as `<productKey>.iot-as-mqtt.eu-central-1.aliyuncs.com` instead of the API host. Only useful if your DNS resolves that name to this host **and** the broker certificate covers it and is trusted by the device; otherwise leave it off. Linux models are never affected. |

Payload capture and proxy mode are **not** options here. Both are things you turn on
while watching a device, so they live in the panel's **Setup → Settings**, take
effect immediately, and persist to `/data/settings_overrides.json`.

## Ports

| Container port | Default host port | Purpose |
|------|------|---------|
| `80` | `80` | Device HTTP API. ESP32 models want it here: a T4 provisioned with `:8080` reaches Wi-Fi and then fails to connect to the server, and one redirected by DNS dials 80 with nowhere to tell it otherwise. |
| `443` | `443` | Device MQTT over TLS. |
| `1883` | *(unmapped)* | Plain MQTT listener, internal to this app. Map it only if a device connects in plaintext. |
| `9000` | `9000` | Media upload bucket the device PUTs photos and videos to. |
| `8099` | *(unmapped)* | The web panel over plain HTTP, **no authentication**. Ingress proxies this internally. |
| `8554` | *(unmapped)* | Camera RTSP from the bundled go2rtc. Home Assistant reaches it over the internal network, so map it only to watch the stream from elsewhere on the LAN — and note 8554 clashes with the go2rtc app. |

Remapping a port is honoured where it can be: the host port `80/tcp` is
published on goes into the address devices are handed, and the same for the
bucket on `9000/tcp`. Two cannot follow. A device redirected by DNS dials port
80 with nothing to tell it otherwise, and the MQTT port comes from the
firmware's own build — so leave `80` and `443` alone unless you know which of
your devices is provisioned with what.

**MQTT port coexistence:** the device-facing broker is a *separate* broker from
Home Assistant's. Its plain listener is unmapped by default, so it cannot clash
with the Mosquitto app.

**The panel is unauthenticated on port 8099.** The sidebar entry goes through
Ingress and is authenticated by Home Assistant; port 8099 is the same
application without that check, which is why it is unmapped by default. Map it
only if you want to `curl` the JSON API while debugging.

Web Bluetooth provisioning needs a secure context. This app deliberately does
not manufacture one for you with a second, self-signed HTTPS port — that would
publish the whole API to the LAN again. Provisioning needs Home Assistant served
over HTTPS with a certificate your browser trusts; the Provision tab explains
this and links a hosted alternative when it is not.

Port 9000 likewise accepts uploads without authentication — it stands in for
Aliyun OSS, whose credentials this app issues to the device itself.

## Networking

The device must resolve `api_url` to this app.

**BLE provisioning works on every model** and is the most reliable route — see
below.

**ESP32 models** (T3/T4/D3/D4/D4S, Feeder, Feeder Mini) can also be redirected
by DNS, because they talk plain HTTP. The BLE-only EverSweet fountains are not on that
list and never will be: they have no network of their own, so there is no name
to redirect (see the README's supported-devices table). The EverSweet Ultra AI
does have Wi-Fi, and is a Linux model — see below. There is no one name to point here: like the Linux
models, they are given their API server during Bluetooth setup, so it is
whichever of PetKit's regional servers the app handed that device. Find it in
your DNS server's query log, or redirect every PetKit domain. The device dials
**port 80** and cannot be told otherwise, so the API has to be reachable there.

Redirecting a name redirects it for this app too, so **proxy mode then
reaches itself instead of the cloud**. It notices and stops forwarding rather
than serving you its own reply labelled as PetKit's. To use proxy mode on such a
network, set *DNS for upstream lookups* in Setup to a resolver that still answers
truthfully — that setting is used for finding the upstream server and nothing
else.

**Linux models** (T5/T6/T7/D4H/D4SH/W7H) enforce HTTPS, so a DNS override alone
will not do; use BLE provisioning, which sidesteps HTTPS entirely. They also
compile the cloud's CA into `ctrl` and so will not trust any other MQTT broker
until it is patched — the panel's **Patchers** tab does that.

Those models are not all the same CPU: W7H is ARM, the rest are Ingenic MIPS.
The patchers that only move files — CA Certificate and Local Camera Streaming —
work on either. MQTT TLS Bypass and Local Storage rewrite machine code, and
Persistent SSH installs a prebuilt binary, so those three are MIPS-only for now
and the tab says so per patch rather than hiding itself.

## The camera in Home Assistant

Apply **Local Camera Streaming** and the bundled go2rtc republishes the
feed as RTSP. The address appears on that patcher's card in the panel and as the
`Camera Stream URL` sensor — copy it from one of those rather than typing it,
because the hostname depends on how this was installed. Paste it into a
**Generic Camera** in Home Assistant.

Two things worth knowing:

- **Do not give Home Assistant the device's own `http://…/main.flv` address.**
  HA opens a stream URL with PyAV, and that one segfaults libav and restarts the
  whole of Home Assistant. The device addresses are listed for VLC and your own
  tooling; go2rtc is there to stand between HA and the device.
- **The device is only streamed from while somebody is watching.** go2rtc dials
  it on the first viewer and drops the connection after the last one leaves, so
  however many people watch, the device sees one connection.
- **It wants a few seconds between connections.** Reopening the stream the
  instant it was closed can fail once; waiting a moment and retrying works. That
  is the device, not go2rtc — it behaves the same when opened directly.

There is no snapshot URL: the device answers every path with the same video
stream, so the MQTT camera entity stays empty and the sensor carries the URL
instead.

## Provisioning over Bluetooth

The **Provision** tab hands a device its Wi-Fi credentials, a custom `apiServers`
URL and a timezone — which is the only way a device ever gets one, so a box
provisioned without it stamps UTC onto its video until it is re-provisioned.

Web Bluetooth only runs on a **secure page**, so this needs Home Assistant served
over HTTPS with a certificate your browser trusts (a reverse proxy, or Home
Assistant's own TLS). With one in place it works from the sidebar as usual,
Ingress included. Over plain HTTP the tab says so and points at a hosted build of
the same page, which runs entirely in your browser and talks to the device over
Bluetooth — your Wi-Fi password is never sent anywhere.

Chrome or Edge only, on desktop or Android. Firefox, Safari and iOS have no Web
Bluetooth at all.

## Troubleshooting

- **No entities appear:** confirm the device reached it (the log
  shows `Signup` / `State report`), and that the MQTT integration is configured.
- **Controls show "unknown":** they render from device settings; they populate
  from defaults immediately and update on the first change.
- **Device shows unavailable:** it hasn't reported within `offline_timeout`.
- **Device connects but then does nothing:** power-cycle it before investigating
  anything else. The firmware can abort its own start-up sequence part-way and
  carry on looking healthy — the process stays alive and the MQTT session
  authenticates normally, so from this side it is indistinguishable from a
  device that simply has nothing to say. Seen on a T5 and on a W7H; a reboot
  fixed both. If it comes back, the device's own `devRun.log` in **Device logs**
  names the file and line that handled each response, which is how the T5's
  cause was found.
- **Wrong sensor values:** the state parser is tuned from limited samples — turn
  on `capture` and open an issue with the collected `*.jsonl` — but read the
  warning under **Getting help** first.

## Getting help

- The **Log** tab shows every request a device makes, and can save it to a file
  to attach to an issue.
- The **Setup** tab has connection diagnostics and the exact values a device
  needs.
- Turning on **Traffic capture** (Setup → Settings) records raw payloads to
  `/data/capture/*.jsonl`. That is the single most useful thing to attach when
  something is wrong for a device model this project has not verified — see the
  supported-device table in the repository README for which those are.

  **⚠ Read a capture before you post it.** A capture is a verbatim recording of
  everything the device said and was told; nothing in it is filtered, because it
  is only useful if it is exact. Any of these files can contain your **Wi-Fi
  SSID**, LAN addresses, the device serial and its signing secret, and if proxy
  mode was on, `proxy_http.jsonl` and `proxy_mqtt.jsonl` also carry the full
  exchanges with PetKit **including your account credentials**. Attach only what
  the question needs.
