# Local changes on top of upstream 1.2.0

This is a local fork of https://github.com/alex-so-3/petkit-local, built to
apply one fix ahead of it landing upstream (if it does).

## dev_signup: accept identity from a form-urlencoded POST body

**File:** `petkit_local/http/handlers/signup.py`

**Problem:** `dev_signup` only read the device's `id` (and `mac`/`firmware`)
from the `X-Device` header or the URL query string. Confirmed on hardware
(PetKit Fresh Element Solo / D4, firmware 1.267) that this device sends its
identity as a `application/x-www-form-urlencoded` POST body instead:

```
POST /6/d4/dev_signup
Content-Type: application/x-www-form-urlencoded

hardware=1&firmware=1.267&mac=c05d89d25204&timezone=2.0&locale=Europe/Amsterdam
&id=400090690&sn=20241223G11497&bt_mac=c05d89d25206&ap_mac=c05d89d25205&chipid=13783556
```

No `X-Device` header, no query string — so `device_id(request)` always
returned `None` and the device was rejected with `{"error": "missing device
id"}` even though its id was right there in the body. This appears to affect
the whole D3/D4/D4s family (ESP32-based feeders), which upstream lists as
"supported, not tested" — this looks like the untested part.

**Fix:** when the header/query resolution comes up empty *and* the request is
form-encoded, fall back to reading `id`/`sn`/`mac`/`firmware` from the POST
body — only for whichever fields are still missing, so a value already
resolved from the trusted `X-Device` header is never overridden by the body.

**Worth doing eventually:** open this as an issue/PR against upstream with
the log line above — this isn't specific to this network, it should affect
every D3/D4/D4s owner.

## heartbeat: ESP32 family uses different msgType numbering than Ingenic

**File:** `petkit_local/http/handlers/heartbeat.py`

**Problem:** `_to_heartbeat_content` translates a queued command into the
`{"msgType": ..., "payload": ..., "type": ..., "timestamp": ...}` shape sent
over the HTTP heartbeat (the fallback path for any device without a live MQTT
session — which, per the missing ESP32 TLS-bypass patcher, is every ESP32
device today). `_SERVICE_MAP`'s numbers were only ever confirmed against T5
(Ingenic) cloud traffic.

Captured real cloud traffic for a `property/set` command (turning off the D4's
indicator light) via proxy mode + payload capture, 2026-08-01:

```json
{"msgType": 1, "payload": {"lightMode": 0}, "timestamp": 1785621658}
```

vs. what the code sent:

```json
{"msgType": 5, "payload": {"lightMode": 0}, "type": "property_set", "timestamp": ...}
```

Wrong `msgType` (1 vs 5) and an extra `type` field the real firmware never
sends. The D4 dispatches on `msgType`, so `5` is presumably unrecognized and
silently dropped — command "delivered" per the log, but nothing happens on
the device.

**Fix:** added `_ESP32_SERVICE_MAP`, consulted instead of `_SERVICE_MAP` for
non-`is_next_gen` devices (ESP32 family: T3/T4/D3/D4/D4S/W4/W5/CTW2/CTW3).
Currently only contains the confirmed `property/set → (1, None)` entry (the
`None` means "omit the `type` key entirely"); every other service falls back
to `_SERVICE_MAP`'s Ingenic numbers, which are **not** confirmed correct for
ESP32 and may need their own hardware capture (e.g. `feed_realtime` for the
feed button, if it's still not working after this patch).

**Worth doing eventually:** capture `start`/`end`/`feed_realtime`/`connect`
against real cloud traffic too, and send the whole thing upstream as one PR
alongside the `dev_signup` fix — same root cause (ESP32 protocol assumed to
match Ingenic, never verified).

