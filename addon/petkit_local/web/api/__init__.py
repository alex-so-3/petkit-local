"""The JSON API behind the panel: one module per group of routes.

This is the operator-facing side of petkit-local, next to (but separate from)
the device-facing API in `http/`. It covers real-device bring-up and day-to-day
control:

* **Devices** — one expandable panel per device from the live `DeviceRegistry`, per-device readable state
  resolved through the same dotted `value_path` HA uses, editable controls and a
  manual command sender (`api_devices`, `api_device_detail`, `api_send_command`).
* **Log** — the in-memory `EventHub` ring of HTTP+MQTT traffic, polled
  (`api_events`) or streamed over a WebSocket (`api_ws`).
* **Timeline** — visit sessions grouped at query time out of the `EventStore`,
  with their media (`api_timeline`).
* **Media** — files and thumbnails out of the friendly media tree
  (`api_media_file`, `api_media_thumb`) and the retention caps (`api_retention`).
* **Live view** — the streaming subset of go2rtc's API proxied onto our origin,
  and TURN credentials for watching from off the LAN (`api_stream_proxy`,
  `api_turn`).
* **Settings** — the runtime settings that can be flipped without a restart
  (`api_settings`), plus per-device capability/AI toggles.
* **Patchers** — apply/remove the on-device binary patches (`api_patcher_*`),
  which run as long-lived background tasks and report progress via the hub.
* **Pets** — CRUD and reference face photos for on-device recognition
  (`api_pets_*`).
* **Provisioning** — the Web Bluetooth page is pure frontend; it talks to the
  device directly from the browser, so there is no route for it here.

Which module owns what
----------------------
`_common.py`    what more than one of the others needs: the app-config readers,
                the `{id}`/JSON-body/`?limit=` parsers, the shared `_deliver`,
                and the pet lookups the timeline shares with pets
`settings.py`   the live-editable runtime settings, `/api/info`, the blocked-
                attempt log and the retention caps
`devices.py`    the device list, the device detail and everything it inlines,
                the manual command sender, the per-device toggles
`schedules.py`  the schedule editors' one write endpoint and its validators
`media.py`      serving the friendly media tree, thumbnails, and the role ->
                slot mapping the Timeline renders from
`timeline.py`   the grouped day view and one event's stored detail
`stream.py`     the go2rtc stream proxy and the Cloudflare TURN mint behind a
                live view
`pets.py`       pet CRUD, reference face photos, and the cloud import
`ble.py`        the BLE accessories: pairing, commands, cloud import
`patchers.py`   applying and removing the on-device binary patches
`logs.py`       the live event ring and WebSocket, the capture browser and the
                uploaded device logs

The route table is NOT here. `web/panel.py` registers all of it in one
function, because three of the registrations are ordering-critical (the media
thumb route, `/api/pets/unbound`, `/api/devicelogs`) and that is only checkable
while they sit together. Nothing in this package imports `panel.py` at module
scope, so the dependency runs one way; the two handlers that do need something
from it import it inside the call and say why.

The panel's JS consumes the exact key names these handlers emit, so response
shapes are a contract: adding a key is safe, renaming or removing one is not.
"""
