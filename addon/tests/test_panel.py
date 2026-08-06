"""Tests for the web panel (EventHub + Ingress JSON API)."""
import asyncio
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urljoin

import pytest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from petkit_local.devices.registry import DeviceRegistry
from petkit_local.devices.ble import BLERegistry
from petkit_local.web.hub import EventHub
from petkit_local.web.panel import LIVE_SETTINGS, create_panel_app


# --- EventHub ---

def test_hub_publish_and_recent():
    hub = EventHub(maxlen=5)
    for i in range(8):
        hub.publish("http", device_id=1, summary=f"e{i}")
    evs = hub.recent()
    assert len(evs) == 5  # ring capped
    assert evs[-1]["summary"] == "e7"


def test_hub_recent_filters_by_device():
    hub = EventHub()
    hub.publish("http", 1, "a")
    hub.publish("http", 2, "b")
    assert [e["summary"] for e in hub.recent(device_id=2)] == ["b"]


def test_hub_diag_records():
    hub = EventHub()
    hub.record_http(5, "POST", "/6/t5/dev_signup", 200)
    hub.set_state_report(5, {"sandPercent": 40})
    hub.record_mqtt(5, "/sys/pk/dn/thing/event/property/post", {"params": {"x": 1}})
    hub.record_connect(5, {"username": "d_t5_SN&pk", "ok": True})
    d = hub.diag(5)
    assert d["http_count"] == 1 and d["mqtt_count"] == 1
    assert d["last_state_report"]["body"]["sandPercent"] == 40
    assert d["last_property"]["payload"]["params"]["x"] == 1
    assert d["last_connect"]["ok"] is True


def test_mqtt_event_carries_an_expandable_detail():
    """A log row is expandable in the panel only when its event has a `detail`;
    an MQTT frame published without one shows a topic and nothing else."""
    hub = EventHub()
    hub.record_mqtt(5, "/sys/pk/d_t5_SN/thing/event/property/post",
                    {"params": {"x": 1}}, client="d_t5_SN")
    ev = hub.recent()[-1]
    assert ev["summary"] == "from d_t5_SN: event/property/post"
    assert ev["detail"]["direction"] == "device → server"
    assert ev["detail"]["client"] == "d_t5_SN"
    assert ev["detail"]["topic"] == "/sys/pk/d_t5_SN/thing/event/property/post"
    assert json.loads(ev["detail"]["payload"]) == {"params": {"x": 1}}


def test_outbound_mqtt_is_logged_but_not_counted_as_device_traffic():
    """`mqtt_count` answers "is this device talking to us" — our own commands
    must not answer yes on its behalf."""
    hub = EventHub()
    hub.record_mqtt(5, "/sys/pk/d_t5_SN/thing/service/property/set",
                    {"method": "thing.service.property.set"},
                    outbound=True, client="d_t5_SN")
    ev = hub.recent()[-1]
    assert ev["summary"] == "to d_t5_SN: service/property/set"
    assert ev["detail"]["direction"] == "server → device"
    assert hub.diag(5)["mqtt_count"] == 0
    assert "last_mqtt" not in hub.diag(5)


def test_a_relayed_cloud_frame_names_the_device_as_its_destination():
    """Proxy mode's downstream frames are outbound but not OURS.

    Passing the cloud as `client` rendered "to the real cloud" for a frame
    arriving FROM it — the direction read exactly backwards in the log.
    """
    hub = EventHub()
    hub.record_mqtt(5, "/sys/pk/d_t5_SN/thing/service/start", {"method": "thing.service.start"},
                    outbound=True, client="d_t5_SN", origin="the real cloud")
    ev = hub.recent()[-1]
    assert ev["summary"] == "to d_t5_SN (relayed from the real cloud): service/start"
    assert ev["detail"]["direction"] == "the real cloud → server → device"
    assert ev["detail"]["origin"] == "the real cloud"


def test_a_wire_payload_is_decoded_not_repred():
    """Proxy mode relays the cloud's frame as bytes; `json.dumps` refuses those,
    and the repr fallback rendered `b'{...}'` — unreadable and no longer JSON
    for the panel to expand."""
    hub = EventHub()
    hub.record_mqtt(5, "/sys/pk/d_t5_SN/thing/service/start", b'{"method": "thing.service.start"}',
                    outbound=True, client="d_t5_SN", origin="the real cloud")
    payload = hub.recent()[-1]["detail"]["payload"]
    assert json.loads(payload) == {"method": "thing.service.start"}


def test_mqtt_payload_is_capped():
    """The ring keeps these in memory and ships each one to every open browser."""
    hub = EventHub()
    hub.record_mqtt(5, "/sys/pk/dn/thing/event/property/post", {"blob": "x" * 20_000})
    payload = hub.recent()[-1]["detail"]["payload"]
    assert len(payload) < 5000
    assert "truncated" in payload


def test_mqtt_payload_that_is_not_json_still_renders():
    hub = EventHub()
    hub.record_mqtt(5, "/sys/pk/dn/thing/event/x/post", {"o": object()})
    assert hub.recent()[-1]["detail"]["payload"]


def test_short_topic_leaves_an_unexpected_shape_alone():
    hub = EventHub()
    hub.record_mqtt(5, "some/other/topic", {})
    assert hub.recent()[-1]["summary"].endswith("some/other/topic")


# --- panel API ---

class FakeBridge:
    def __init__(self, connected=False):
        self._client = object() if connected else None
        self.sent = []

    async def publish_to_device(self, device, suffix, payload):
        self.sent.append((device.petkit_id, suffix, payload))


def _panel(reg=None, ble=None, bridge=None, cfg=None):
    reg = reg or DeviceRegistry()
    ble = ble or BLERegistry()
    hub = EventHub()
    cfg = cfg or {"api_url": "http://x/6/", "mqtt_tls": True,
                  "mqtt_tls_port": 443,
                  "capture": False, "capture_dir": "/nope"}
    return create_panel_app(reg, ble, hub, cfg, bridge), reg, hub


async def _mk_client(app):
    c = TestClient(TestServer(app))
    await c.start_server()
    return c


async def test_index_served():
    app, reg, hub = _panel()
    c = await _mk_client(app)
    try:
        r = await c.get("/")
        assert r.status == 200
        assert "PetKit Local" in await r.text()
    finally:
        await c.close()


def _asset_hrefs(html: str) -> list[str]:
    """Every EXTERNAL `href=`/`src=` value the index page pulls in.

    In-document fragments (`href="#..."`, used by the cropper's SVG guide) are
    not assets — nothing is fetched for them. Excluding them keeps this test
    about the property it exists for: the page loads no CDN and no third-party
    resource, which is what the artifact CSP and the offline-first design need.
    """
    return [h for h in re.findall(r'(?:href|src)="([^"]+)"', html)
            if not h.startswith("#")]


async def test_index_renders_template_and_links_assets():
    app, reg, hub = _panel()
    c = await _mk_client(app)
    try:
        html = await (await c.get("/")).text()
        # Markup survived the extraction...
        assert '<section id="tab-timeline"' in html and "Provision via Bluetooth" in html
        # ...and the CSS/JS are now external, not inline.
        assert "<style>" not in html and "<script>\n" not in html
        # RELATIVE, so they resolve under whatever opaque prefix Ingress uses;
        # an absolute /static/... would escape it and 404. The `?v=` is
        # cache-busting and must not turn them into absolute URLs.
        from petkit_local.web import panel as panel_mod
        assert _asset_hrefs(html) == [
            f"static/styles.css?v={panel_mod.ASSET_VERSION}",
            f"static/app.js?v={panel_mod.ASSET_VERSION}",
        ]
    finally:
        await c.close()


async def test_static_assets_are_served():
    app, reg, hub = _panel()
    c = await _mk_client(app)
    try:
        css = await c.get("/static/styles.css")
        assert css.status == 200 and css.headers["Content-Type"].startswith("text/css")
        assert "--accent" in await css.text()
        js = await c.get("/static/app.js")
        assert js.status == 200 and "javascript" in js.headers["Content-Type"]
        assert "const BASE = location.pathname" in await js.text()
    finally:
        await c.close()


async def test_index_assets_resolve_under_an_ingress_prefix():
    # Home Assistant mounts the panel at an opaque /api/hassio_ingress/<token>/
    # path. Asset URLs must be *relative* so the browser resolves them under
    # that prefix — an absolute /static/... would escape it and 404.
    prefix = "/api/hassio_ingress/K7d2Xq"
    panel, reg, hub = _panel()
    outer = web.Application()
    outer.add_subapp(prefix, panel)
    c = await _mk_client(outer)
    try:
        doc = prefix + "/"
        html = await (await c.get(doc)).text()
        for href in _asset_hrefs(html):
            assert not href.startswith("/"), href
            resolved = urljoin(doc, href)
            assert resolved.startswith(doc), resolved
            r = await c.get(resolved)
            assert r.status == 200, (resolved, r.status)
    finally:
        await c.close()


async def test_index_is_only_routed_at_a_trailing_slash_path():
    # Relative asset URLs are only safe because the document path always ends
    # in "/": the page has exactly one route and it is "/".
    app, reg, hub = _panel()
    c = await _mk_client(app)
    try:
        assert (await c.get("/index.html")).status == 404
        assert (await c.get("/panel")).status == 404
    finally:
        await c.close()


async def test_frontend_has_no_inline_event_handlers():
    # The XSS vector this replaced: device-derived strings reach innerHTML, and
    # an apostrophe in one closed an onclick='...' and opened a real attribute.
    # Behaviour is now bound through the [data-action]/[data-change] delegation
    # table, so any reintroduced on*= attribute is a regression.
    app, reg, hub = _panel()
    c = await _mk_client(app)
    try:
        js = await (await c.get("/static/app.js")).text()
        html = await (await c.get("/")).text()
        inline = re.compile(r"""\bon[a-z]+\s*=\s*["']""", re.I)
        assert not inline.search(js), inline.search(js).group(0)
        assert not inline.search(html), inline.search(html).group(0)
        assert "data-action" in html and "data-action" in js
    finally:
        await c.close()


async def test_devices_tab_is_one_collapsible_panel_per_device():
    """The grid plus a single shared detail pane is gone. That pane is why the
    `<details>` sections inside it collapsed a few seconds after being opened:
    one global refresh reassigned its innerHTML and took their open state with
    it."""
    app, reg, hub = _panel()
    c = await _mk_client(app)
    try:
        html = await (await c.get("/")).text()
        js = await (await c.get("/static/app.js")).text()
        assert 'id="devPanels"' in html
        assert 'id="devGrid"' not in html and 'id="devDetail"' not in html
        # Open state is remembered per device AND per section, or expanding
        # diagnostics on one device would expand it on all of them.
        assert "devOpen" in js and "data-toggle" in js
        # `toggle` does not bubble, so the listener must be in the capture phase.
        assert "addEventListener('toggle'" in js and "true)" in js
    finally:
        await c.close()


async def test_no_element_id_is_shared_between_device_panels():
    """With one pane there was exactly one #cmdOut and one #cmdRaw. With a panel
    per device those ids would collide and every device's result would land in
    the first panel."""
    app, reg, hub = _panel()
    c = await _mk_client(app)
    try:
        js = await (await c.get("/static/app.js")).text()
        assert "id=\"cmdOut\"" not in js and "getElementById('cmdOut')" not in js
        assert "id=\"cmdRaw\"" not in js and "getElementById('cmdRaw')" not in js
        assert "cmd-out" in js and "cmd-raw" in js
    finally:
        await c.close()


async def test_every_published_entity_component_has_somewhere_to_render():
    """The anti-divergence check. `text`, `event`, `camera` and `image` entities
    were published to Home Assistant and rendered nowhere in the panel, which is
    invisible until someone goes looking for a schedule editor that does not
    exist. Any NEW component must be routed too."""
    from petkit_local.devices.ble import BLE_TYPES, get_ble_entities
    from petkit_local.devices.categories import CATEGORY_SPECS

    published = {e.component for spec in CATEGORY_SPECS.values()
                 for e in spec.entities_for(True)}
    # BLE accessories publish to HA through a different table entirely
    # (`get_ble_entities`), and this guard used to look only at the device one.
    # A CTW3 brought switches, selects and numbers in through that gap.
    published |= {e.component for t in BLE_TYPES for e in get_ble_entities(t)}
    app, reg, hub = _panel()
    c = await _mk_client(app)
    try:
        js = await (await c.get("/static/app.js")).text()
        table = js[js.index("const ENTITY_SECTION = {"):]
        table = table[: table.index("};")]
        for component in sorted(published):
            assert f"{component}:" in table, (
                f"{component} entities are published to HA but ENTITY_SECTION "
                f"in app.js does not route them to a card"
            )
    finally:
        await c.close()


async def test_the_accessory_panel_renders_every_component_an_accessory_has():
    """`ENTITY_SECTION` routes a device panel. An accessory has its own
    renderer, `accBody`, which picks components by name into three cards — so a
    component that reaches HA fine can still land nowhere here. The filter
    reset was the first: `button` was routed by ENTITY_SECTION and dropped by
    accBody, which knew only switch, number, select and the two sensors."""
    from petkit_local.devices.ble import BLE_TYPES, get_ble_entities

    app, reg, hub = _panel()
    c = await _mk_client(app)
    try:
        js = await (await c.get("/static/app.js")).text()
        body = js[js.index("function accBody("):]
        body = body[: body.index("\nfunction ")]
        for t in BLE_TYPES:
            for e in get_ble_entities(t):
                assert f"'{e.component}'" in body, (
                    f"a {t} publishes a {e.component} entity and accBody has "
                    f"no card that collects one"
                )
    finally:
        await c.close()


async def test_device_detail_answers_in_one_request():
    """The sidecars are folded in, so a panel refresh is one round trip. They
    must stay identical to their standalone endpoints, which the API still
    exposes."""
    reg = DeviceRegistry()
    reg.get_or_create(petkit_id=7, device_type="t5", serial_number="SN7")
    app, reg, hub = _panel(reg=reg)
    c = await _mk_client(app)
    try:
        detail = await (await c.get("/api/devices/7")).json()
        assert detail["capInfo"] == await (await c.get("/api/devices/7/capabilities")).json()
        assert detail["logInfo"] == await (await c.get("/api/devices/7/logs")).json()
        assert detail["aiInfo"] == await (await c.get("/api/devices/7/ai")).json()
        # Grouping config/diagnostic the way HA does needs this on every entity.
        assert all("entity_category" in e for e in detail["entities"])
    finally:
        await c.close()


async def test_a_device_with_no_camera_or_ai_gets_null_sidecars():
    reg = DeviceRegistry()
    reg.get_or_create(petkit_id=8, device_type="t4", serial_number="SN8")
    app, reg, hub = _panel(reg=reg)
    c = await _mk_client(app)
    try:
        detail = await (await c.get("/api/devices/8")).json()
        assert detail["capInfo"] is None and detail["aiInfo"] is None
        assert detail["logInfo"] is not None
    finally:
        await c.close()


async def test_esc_escapes_every_markup_breaking_character():
    # esc() guards text nodes and double-quoted attributes; it must cover the
    # quote characters too, not just angle brackets, or an attribute can be
    # closed from inside a value.
    app, reg, hub = _panel()
    c = await _mk_client(app)
    try:
        js = await (await c.get("/static/app.js")).text()
        for ch, ent in [("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"),
                        ('"', "&quot;"), ("'", "&#39;"), ("/", "&#x2F;")]:
            assert ent in js, f"esc() does not map {ch!r} to {ent}"
    finally:
        await c.close()


async def test_info_and_devices():
    reg = DeviceRegistry()
    reg.get_or_create(petkit_id=1, device_type="t5", serial_number="SN")
    app, reg, hub = _panel(reg=reg)
    c = await _mk_client(app)
    try:
        info = await (await c.get("/api/info")).json()
        assert info["mqtt_tls"] is True and info["device_count"] == 1
        devs = await (await c.get("/api/devices")).json()
        assert devs[0]["id"] == 1 and devs[0]["type"] == "t5"
        detail = await (await c.get("/api/devices/1")).json()
        assert "state" in detail and "settings" in detail
        assert any(a["key"] == "cleaning_start" for a in detail["actions"])
    finally:
        await c.close()


async def test_only_costly_actions_are_flagged_destructive_and_they_sort_last():
    """`destructive` drives three things at once: the red styling, the confirm
    dialog, and the ordering. So it has to mean "costs something you cannot get
    back", not "moves the motor".

    `reset` and `maintenance_stop` used to be in the set and are the clearest
    case against it: both are `thing.service.end`, the verbs that STOP whatever
    is running and put the box back in service. Making the recovery button look
    and behave exactly like the one you are recovering from is backwards, and
    it put two red buttons in the middle of a row of eleven.
    """
    from petkit_local.devices.registry import DeviceRegistry
    reg = DeviceRegistry()
    reg.get_or_create(petkit_id=1, device_type="t5", serial_number="SN")
    app, reg, hub = _panel(reg=reg)
    c = await _mk_client(app)
    try:
        actions = (await (await c.get("/api/devices/1")).json())["actions"]
        red = {a["key"] for a in actions if a["destructive"]}
        assert red == {"dump_litter", "maintenance_start", "reset_n50", "reset_n60"}

        # Recovery verbs must stay safe-looking and un-confirmed.
        safe = {a["key"] for a in actions if not a["destructive"]}
        assert {"reset", "maintenance_stop", "cleaning_start", "resume"} <= safe

        # Destructive last, so a mis-click lands on something harmless.
        flags = [a["destructive"] for a in actions]
        assert flags == sorted(flags), f"destructive actions are interleaved: {flags}"
    finally:
        await c.close()


async def test_command_sender_queues_without_bridge():
    reg = DeviceRegistry()
    reg.get_or_create(petkit_id=1, device_type="t5", serial_number="SN")
    app, reg, hub = _panel(reg=reg, bridge=None)
    c = await _mk_client(app)
    try:
        r = await c.post("/api/devices/1/command", data=json.dumps({"action": "cleaning_start"}))
        out = await r.json()
        assert out["ok"] and out["delivered"] == "heartbeat-queue"
        assert reg.get(1).command_queue  # queued
    finally:
        await c.close()


async def test_command_sender_uses_bridge_when_connected():
    reg = DeviceRegistry()
    reg.get_or_create(petkit_id=1, device_type="t5", serial_number="SN")
    reg.get(1).mqtt_connected = True  # device has a live MQTT session
    bridge = FakeBridge(connected=True)
    app, reg, hub = _panel(reg=reg, bridge=bridge)
    c = await _mk_client(app)
    try:
        r = await c.post("/api/devices/1/command", data=json.dumps({"action": "cleaning_start"}))
        out = await r.json()
        assert out["delivered"] == "mqtt"
        assert bridge.sent and bridge.sent[0][1] == "start"
    finally:
        await c.close()


async def test_unknown_device_type_lists_and_is_named_unknown():
    # A device with a codename we don't recognize must still register, list,
    # and be reachable — just with no entities and an "Unknown" display name.
    reg = DeviceRegistry()
    reg.get_or_create(petkit_id=99, device_type="zz9", serial_number="SNZ")
    app, reg, hub = _panel(reg=reg)
    c = await _mk_client(app)
    try:
        devs = await (await c.get("/api/devices")).json()
        d = next(x for x in devs if x["id"] == 99)
        assert d["name"] == "Unknown" and d["type"] == "zz9"
        detail = await (await c.get("/api/devices/99")).json()
        assert detail["entities"] == [] and detail["actions"] == []
    finally:
        await c.close()


async def test_command_falls_back_to_heartbeat_when_device_not_on_mqtt():
    # Bridge is connected to the broker, but the DEVICE has no MQTT session
    # (mqtt_connected False) — command must queue for the HTTP heartbeat.
    reg = DeviceRegistry()
    reg.get_or_create(petkit_id=1, device_type="t5", serial_number="SN")
    bridge = FakeBridge(connected=True)
    app, reg, hub = _panel(reg=reg, bridge=bridge)
    c = await _mk_client(app)
    try:
        r = await c.post("/api/devices/1/command", data=json.dumps({"action": "cleaning_start"}))
        out = await r.json()
        assert out["delivered"] == "heartbeat-queue"
        assert not bridge.sent  # never went to MQTT
        queued = reg.get(1).command_queue[0]
        assert queued["_service_suffix"] == "start"  # tagged for heartbeat conversion
    finally:
        await c.close()


async def test_command_entity_setting_updates_and_queues():
    reg = DeviceRegistry()
    reg.get_or_create(petkit_id=1, device_type="t5", serial_number="SN")
    app, reg, hub = _panel(reg=reg, bridge=None)
    c = await _mk_client(app)
    try:
        r = await c.post("/api/devices/1/command",
                         data=json.dumps({"entity": "auto_work", "value": "OFF"}))
        out = await r.json()
        assert out["ok"] and out["delivered"] == "heartbeat-queue"
        # optimistic settings update applied (auto_work -> settings.autoWork)
        assert reg.get(1).config["settings"]["autoWork"] == 0
        queued = reg.get(1).command_queue[0]
        assert queued["_service_suffix"] == "property/set"
        assert queued["params"]["autoWork"] == 0
    finally:
        await c.close()


async def test_settings_live_update_mutates_shared_config():
    # The panel edits the SAME dict the device-facing handlers read, so a proxy
    # toggle takes effect with no restart.
    reg = DeviceRegistry()
    ble = BLERegistry()
    hub = EventHub()
    live = {"proxy_mode": False, "proxy_upstream": "", "proxy_block_run_cmd": True, "capture": False}
    cfg = {"api_url": "http://x/6/", "capture": False, "capture_dir": "/nope"}
    app = create_panel_app(reg, ble, hub, cfg, None, live_config=live)
    c = await _mk_client(app)
    try:
        s = await (await c.get("/api/settings")).json()
        assert s["writable"] is True and s["settings"]["proxy_mode"] is False
        r = await (await c.post("/api/settings", data=json.dumps({"proxy_mode": True, "capture": True}))).json()
        assert r["ok"] and r["changed"]["proxy_mode"] is True
        assert live["proxy_mode"] is True and live["capture"] is True  # shared dict mutated live
        # capture flag now reflected everywhere it's read
        info = await (await c.get("/api/info")).json()
        assert info["capture"] is True and info["settings"]["proxy_mode"] is True
    finally:
        await c.close()


async def test_settings_not_writable_without_live_config():
    app, reg, hub = _panel()  # no live_config -> empty fallback
    c = await _mk_client(app)
    try:
        s = await (await c.get("/api/settings")).json()
        assert s["writable"] is False
        r = await c.post("/api/settings", data=json.dumps({"proxy_mode": True}))
        assert r.status == 400
    finally:
        await c.close()


async def test_ws_streams_live_events():
    import asyncio
    app, reg, hub = _panel()
    c = await _mk_client(app)
    try:
        ws = await c.ws_connect("/api/ws")
        hub.publish("mqtt", 1, "hello-live")
        got = False
        for _ in range(20):
            msg = await asyncio.wait_for(ws.receive_json(), timeout=2)
            if msg.get("summary") == "hello-live":
                got = True
                break
        assert got
        await ws.close()
    finally:
        await c.close()


async def test_provision_ui_has_ble_protocol():
    app, reg, hub = _panel()
    c = await _mk_client(app)
    try:
        html = await (await c.get("/")).text()
        assert "Provision via Bluetooth" in html
        js = await (await c.get("/static/app.js")).text()
        # PetKit Ingenic framed JSON.
        assert "0000aaa0-0000-1000-8000-00805f9b34fb" in js  # service
        assert "0000aaa2-0000-1000-8000-00805f9b34fb" in js  # RX (write)
        assert "0000aaa1-0000-1000-8000-00805f9b34fb" in js  # TX (notify)
        # PetKit ESP32 custom-data transport.
        assert "0000ffff-0000-1000-8000-00805f9b34fb" in js  # service
        assert "0000ff01-0000-1000-8000-00805f9b34fb" in js  # app -> ESP32
        assert "0000ff02-0000-1000-8000-00805f9b34fb" in js  # ESP32 -> app
        # Which one a device speaks is asked, not assumed from a model table --
        # and asked BY NAME, per service. `getPrimaryServices()` returns what
        # the browser has already discovered for the device, which is not what
        # it hands over when asked directly: a D4SH that provisions through
        # `getPrimaryService(0xAAA0)` was absent from the enumeration, so 1.5.0
        # refused to pair a feeder that 1.4.0 paired fine. The enumeration may
        # be read in exactly one place -- describing a device that answered to
        # neither service -- and may never decide which protocol to speak.
        assert "open(BLE_SERVICE)" in js
        assert "open(BLUFI_SERVICE)" in js
        assert js.count("gatt.getPrimaryServices()") == 1
        # Quoted or bare, spaced or not — the payload key is what matters.
        assert re.search(r"""["']?key["']?\s*:\s*151""", js)
        # The decoders themselves are exercised for real in test_provision_js.
        assert "function pkParse(" in js and "function pkCrc16(" in js
        # ESP32 devices answer with PetKit documents inside custom data, and no
        # native Wi-Fi provisioning constants should be present to tempt callers.
        assert "BLUFI_DATA_CUSTOM" in js.split("function blufiExplain(")[1][:1200]
        for never in ("SET_WIFI_OPMODE", "STA_SSID", "STA_PASSWD", "CONN_TO_AP"):
            assert never not in js, never
        for timeout in ("const T_IDENT = 5000", "const T_ACK = 10000",
                        "const T_JOIN = 120000"):
            assert timeout in js, timeout
        # The self-signed HTTPS panel on 8098 is gone — it published this
        # whole unauthenticated API to the LAN. Nothing may hand out that port.
        info = await (await c.get("/api/info")).json()
        assert "web_tls_port" not in info
        assert "8098" not in js
    finally:
        await c.close()


async def test_capture_disabled_message():
    app, reg, hub = _panel()
    c = await _mk_client(app)
    try:
        cap = await (await c.get("/api/capture")).json()
        assert cap["enabled"] is False
    finally:
        await c.close()


async def test_the_timeline_filters_by_pet_with_chips_not_a_dropdown():
    """A `<select>` sat on its own line under the device picker and looked like
    an afterthought. The pet filter is now the same chip group the kind filters
    use, each chip carrying that pet's mugshot — the same face the cards below
    it show, so the filter and the results are visibly about one animal."""
    app, reg, hub = _panel()
    c = await _mk_client(app)
    try:
        js = await (await c.get("/static/app.js")).text()
        assert 'id="tlPet"' not in js, "the pet dropdown is back"
        assert 'data-action="tl-pet"' in js and "petChip" in js
        # The mugshot comes from the pets API's own face list, not a second
        # source that could disagree with the cards.
        assert "p.faces" in js and "chip-av" in js
        # A face that 404s must hide rather than leave a broken-image glyph.
        assert "hide-on-error" in js
        css = await (await c.get("/static/styles.css")).text()
        assert ".chip-av" in css
    finally:
        await c.close()


async def test_the_capture_tab_warns_what_is_in_a_capture():
    """The panel offers a download button for files that can contain the user's
    Wi-Fi SSID and, in proxy mode, their PetKit account credentials — so this
    warning is the only thing between a user and posting their credentials to a
    public issue.

    It asserts on what the warning SAYS, not on the word "redaction": that is an
    internal mechanism, and naming it at a user explains nothing.
    """
    app, reg, hub = _panel()
    c = await _mk_client(app)
    try:
        js = await (await c.get("/static/app.js")).text()
        assert "Every capture is sensitive" in js
        assert "SSID" in js and "credential" in js.lower()
        # No per-file "sensitive" badge: singling some out implies the rest are
        # safe to post, and none of them are.
        assert "SENSITIVE_CAPTURES" not in js
    finally:
        await c.close()


async def test_the_capture_tab_no_longer_describes_capture_as_an_addon_option():
    """It is a live panel setting: there is no `capture` add-on option, no
    `--capture` flag, and no restart involved. The old copy said all three."""
    app, reg, hub = _panel()
    c = await _mk_client(app)
    try:
        js = await (await c.get("/static/app.js")).text()
        assert "--capture" not in js
        assert "restart the add-on" not in js
    finally:
        await c.close()


# --- capture files: delete -------------------------------------------------
# Nothing prunes captures — they have no retention sweep, deliberately — so
# deleting is the only way to reclaim the space. The name comes from a URL, so
# most of what matters here is that it cannot escape the capture directory.

def _capture_app(tmp):
    cfg = {"api_url": "http://x/6/", "capture": True, "capture_dir": str(tmp)}
    live = {"proxy_mode": False, "proxy_upstream": "", "proxy_block_run_cmd": True,
            "capture": True}
    return create_panel_app(DeviceRegistry(), BLERegistry(), EventHub(), cfg, None,
                            live_config=live)


async def test_a_capture_file_can_be_deleted_and_stops_being_listed():
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "http.jsonl").write_text('{"a": 1}\n')
        (Path(tmp) / "mqtt.jsonl").write_text('{"b": 2}\n')
        c = await _mk_client(_capture_app(tmp))
        try:
            listed = await (await c.get("/api/capture")).json()
            assert {f["name"] for f in listed["files"]} == {"http.jsonl", "mqtt.jsonl"}

            r = await c.delete("/api/capture/http.jsonl")
            assert (await r.json())["ok"] is True
            assert not (Path(tmp) / "http.jsonl").exists()

            listed = await (await c.get("/api/capture")).json()
            assert {f["name"] for f in listed["files"]} == {"mqtt.jsonl"}
        finally:
            await c.close()


async def test_deleting_a_capture_that_is_not_there_is_a_404_not_a_crash():
    with tempfile.TemporaryDirectory() as tmp:
        c = await _mk_client(_capture_app(tmp))
        try:
            assert (await c.delete("/api/capture/nope.jsonl")).status == 404
            # Deleting twice: the second call must behave like the first miss.
            (Path(tmp) / "x.jsonl").write_text("{}\n")
            assert (await c.delete("/api/capture/x.jsonl")).status == 200
            assert (await c.delete("/api/capture/x.jsonl")).status == 404
        finally:
            await c.close()


@pytest.mark.parametrize("name", [
    "..%2F..%2Fetc%2Fpasswd.jsonl",   # traversal, percent-encoded
    "..%2Fsecret.jsonl",
    "%2Fetc%2Fpasswd.jsonl",          # absolute path
])
async def test_delete_cannot_escape_the_capture_directory(name):
    with tempfile.TemporaryDirectory() as tmp:
        outside = Path(tmp) / "outside"
        outside.mkdir()
        target = outside / "secret.jsonl"
        target.write_text("do not delete me")
        capture_dir = Path(tmp) / "capture"
        capture_dir.mkdir()
        c = await _mk_client(_capture_app(capture_dir))
        try:
            r = await c.delete("/api/capture/" + name)
            assert r.status == 404
            assert target.exists(), "a traversing name deleted a file outside the capture dir"
        finally:
            await c.close()


async def test_delete_refuses_anything_that_is_not_a_capture_file():
    """The extension is this endpoint's contract — the listing shows only
    `.jsonl`, so nothing else may be deletable through it."""
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "petkit.db").write_text("not a capture")
        c = await _mk_client(_capture_app(tmp))
        try:
            assert (await c.delete("/api/capture/petkit.db")).status == 404
            assert (Path(tmp) / "petkit.db").exists()
        finally:
            await c.close()


async def test_the_capture_tab_offers_delete_next_to_download():
    app, reg, hub = _panel()
    c = await _mk_client(app)
    try:
        js = await (await c.get("/static/app.js")).text()
        assert 'data-action="delete-capture"' in js
        # Irreversible, so it must confirm first.
        assert "deleteCapture" in js and "confirm(" in js
        assert "method: 'DELETE'" in js
    finally:
        await c.close()


# --- settings persistence ---

def _settings_app(tmp):
    live = {"proxy_mode": False, "proxy_upstream": "", "proxy_block_run_cmd": True, "capture": False}
    cfg = {"api_url": "http://x/6/", "capture": False, "capture_dir": "/nope",
           "settings_path": str(Path(tmp) / "overrides.json")}
    return create_panel_app(DeviceRegistry(), BLERegistry(), EventHub(), cfg, None,
                            live_config=live)


async def test_settings_overrides_written_atomically_and_merged():
    # The write must leave no partial file behind and must keep keys set by an
    # earlier POST.
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "overrides.json"
        c = await _mk_client(_settings_app(tmp))
        try:
            await c.post("/api/settings", data=json.dumps({"proxy_mode": True}))
            assert json.loads(path.read_text()) == {"proxy_mode": True}
            await c.post("/api/settings", data=json.dumps({"capture": True}))
            assert json.loads(path.read_text()) == {"proxy_mode": True, "capture": True}
            # no temp file survived the write-then-rename
            assert os.listdir(tmp) == ["overrides.json"]
        finally:
            await c.close()


async def test_settings_overrides_repair_a_corrupt_file():
    # A truncated overrides file is ignored by config.apply_panel_overrides, so
    # the panel rewrites it instead of refusing to persist (the old behaviour).
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "overrides.json"
        path.write_text('{"proxy_mode": tr')
        c = await _mk_client(_settings_app(tmp))
        try:
            r = await c.post("/api/settings", data=json.dumps({"proxy_mode": True}))
            assert (await r.json())["ok"] is True
            assert json.loads(path.read_text()) == {"proxy_mode": True}
        finally:
            await c.close()


# --- ?limit= handling ---

async def test_events_limit_ignores_junk_and_clamps():
    app, reg, hub = _panel()
    for i in range(5):
        hub.publish("http", 1, f"e{i}")
    c = await _mk_client(app)
    try:
        # non-numeric used to raise ValueError -> HTTP 500
        r = await c.get("/api/events?limit=abc")
        assert r.status == 200 and len(await r.json()) == 5
        assert len(await (await c.get("/api/events?limit=2")).json()) == 2
        # clamped high: still the whole ring, never an error
        assert len(await (await c.get("/api/events?limit=99999999")).json()) == 5
        # negative used to slice from the FRONT (evs[3:]); now clamped to 1
        assert len(await (await c.get("/api/events?limit=-3")).json()) == 1
        # a non-numeric device filter is ignored, as before
        assert len(await (await c.get("/api/events?device=xx")).json()) == 5
    finally:
        await c.close()


def _capture_cfg(tmp):
    return {"api_url": "http://x/6/", "capture": True, "capture_dir": str(tmp)}


async def test_capture_read_tails_file_and_reports_full_total():
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "http.jsonl").write_text(
            "".join(json.dumps({"i": i}) + "\n" for i in range(10)))
        app, reg, hub = _panel(cfg=_capture_cfg(tmp))
        c = await _mk_client(app)
        try:
            body = await (await c.get("/api/capture/http.jsonl?limit=3")).json()
            assert body["total"] == 10  # total counts the file, not the page
            assert [rec["i"] for rec in body["records"]] == [7, 8, 9]

            # junk limit falls back to the default instead of raising
            r = await c.get("/api/capture/http.jsonl?limit=oops")
            assert r.status == 200
            body2 = await r.json()
            assert body2["total"] == 10 and len(body2["records"]) == 10
        finally:
            await c.close()


def test_capture_path_helper_contains_and_filters():
    with tempfile.TemporaryDirectory() as tmp:
        from petkit_local.web.panel import _safe_capture_path
        cap = Path(tmp) / "capture"
        cap.mkdir()
        (cap / "ok.jsonl").write_text("{}\n")
        (Path(tmp) / "secret.jsonl").write_text("{}\n")
        app, reg, hub = _panel(cfg=_capture_cfg(cap))

        def resolve(name):
            return _safe_capture_path(
                make_mocked_request("GET", "/api/capture/x", app=app, match_info={"name": name}))

        assert resolve("ok.jsonl") == os.path.realpath(str(cap / "ok.jsonl"))
        assert resolve("../secret.jsonl") is None      # containment
        assert resolve("/etc/hosts.jsonl") is None     # absolute is read as relative
        assert resolve("ok.txt") is None               # endpoint serves .jsonl only
        assert resolve("missing.jsonl") is None


# --- background tasks ---

async def test_background_task_is_tracked_and_cancelled_on_cleanup():
    from petkit_local.web.panel import BACKGROUND_TASKS, _spawn_background, cancel_background_tasks
    app, reg, hub = _panel()
    started = asyncio.Event()

    async def _forever():
        started.set()
        await asyncio.sleep(3600)

    task = _spawn_background(app, _forever(), name="test-forever")
    await asyncio.wait_for(started.wait(), timeout=2)
    assert task in app[BACKGROUND_TASKS]  # strong reference held, not GC-able

    await cancel_background_tasks(app)
    assert task.cancelled()
    assert not app[BACKGROUND_TASKS]


async def test_background_task_exception_is_retrieved():
    from petkit_local.web.panel import BACKGROUND_TASKS, _spawn_background
    app, reg, hub = _panel()

    async def _boom():
        raise RuntimeError("nope")

    panel_log = logging.getLogger("petkit_local.web.panel")
    panel_log.disabled = True  # the failure is logged; keep the test output clean
    try:
        task = _spawn_background(app, _boom(), name="test-boom")
        await asyncio.sleep(0.05)
    finally:
        panel_log.disabled = False
    assert task.done() and isinstance(task.exception(), RuntimeError)
    assert task not in app[BACKGROUND_TASKS]


async def test_patcher_run_is_tracked_and_drained_by_app_cleanup():
    """A patcher run outlives its request; it must be pinned and cancelled at
    shutdown rather than left to the garbage collector."""
    import petkit_local.web.panel as panel

    reg = DeviceRegistry()
    reg.get_or_create(petkit_id=1, device_type="t5", serial_number="SN").state["ip"] = "192.0.2.10"
    app, reg, hub = _panel(reg=reg)
    started = asyncio.Event()

    async def _slow_apply(*args, **kwargs):
        started.set()
        await asyncio.sleep(3600)

    original = panel._patcher_apply
    panel._patcher_apply = _slow_apply
    c = await _mk_client(app)
    try:
        r = await c.post("/api/devices/1/patcher", data=json.dumps({"patcher": "mqtt"}))
        assert (await r.json())["ok"] is True
        await asyncio.wait_for(started.wait(), timeout=2)
        tasks = list(app[panel.BACKGROUND_TASKS])
        assert len(tasks) == 1
    finally:
        panel._patcher_apply = original
        await c.close()  # AppRunner cleanup -> on_cleanup -> cancel_background_tasks
    assert tasks[0].cancelled()


async def test_the_stream_addresses_live_on_the_device_not_on_the_patcher():
    """The Patchers card is about applying and undoing a change to the firmware.
    Once applied, where to watch the device is just another fact about it, so it
    belongs in the device view — and the patcher endpoint must not grow it back."""
    reg = DeviceRegistry()
    d = reg.get_or_create(petkit_id=1, device_type="t5", serial_number="SN")
    d.state["ip"] = "192.0.2.10"
    d.state["streamAvailable"] = True
    app, reg, hub = _panel(reg=reg)

    class _Sidecar:
        def stream_url_for(self, device):
            return "rtsp://addon-host:8554/1"

    app["go2rtc"] = _Sidecar()
    c = await _mk_client(app)
    try:
        detail = await (await c.get("/api/devices/1")).json()
        assert detail["streams"]["rtsp"] == "rtsp://addon-host:8554/1"
        # The device's own address is offered too, but never first: it is the
        # one that crashes Home Assistant.
        assert detail["streams"]["flv"].startswith("http://192.0.2.10/")
        assert list(detail["streams"])[0] == "rtsp"

        patcher = await (await c.get("/api/devices/1/patcher")).json()
        assert "streams" not in patcher["patchers"]["camera"]
    finally:
        await c.close()


async def test_a_device_with_no_confirmed_stream_is_offered_no_address():
    reg = DeviceRegistry()
    reg.get_or_create(petkit_id=1, device_type="t5", serial_number="SN").state["ip"] = "192.0.2.10"
    app, reg, hub = _panel(reg=reg)
    c = await _mk_client(app)
    try:
        assert (await (await c.get("/api/devices/1")).json())["streams"] == {}
    finally:
        await c.close()


async def test_all_patchers_are_offered_on_every_next_gen_device():
    """No patcher is gated by architecture in the listing. The patchers
    themselves validate the binary they download from the device."""
    reg = DeviceRegistry()
    reg.get_or_create(petkit_id=1, device_type="w7h", serial_number="SN")
    reg.get_or_create(petkit_id=2, device_type="t5", serial_number="SN2")
    app, reg, hub = _panel(reg=reg)
    c = await _mk_client(app)
    try:
        for did in (1, 2):
            resp = await (await c.get(f"/api/devices/{did}/patcher")).json()
            assert resp["supported"] is True
            blocked = {p for p, e in resp["patchers"].items() if e["unavailable"]}
            assert blocked == set(), f"device {did} has blocked patchers: {blocked}"
    finally:
        await c.close()


async def test_assets_are_revalidated_not_heuristically_cached():
    """Without Cache-Control a browser may reuse a cached asset WITHOUT asking.

    That is not theoretical: after an add-on update the panel kept running the
    previous app.js, so a deployed feature was invisible with nothing in the
    logs to say so. `no-cache` still permits storage — the ETag makes the
    revalidation a 304 — it only forbids using it unasked.
    """
    app, _reg, _hub = _panel()
    c = await _mk_client(app)
    try:
        for path in ("/", "/static/app.js", "/static/styles.css"):
            r = await c.get(path)
            assert r.status == 200, path
            assert r.headers.get("Cache-Control") == "no-cache", path
        # The ETag survives, so revalidation stays cheap.
        r = await c.get("/static/app.js")
        assert r.headers.get("ETag")
    finally:
        await c.close()


async def test_the_index_says_no_cache_whatever_its_path_looks_like():
    """The header used to come from a path match — `endswith("/")` — and the
    index is the one response that cannot afford to depend on that. It is what
    names the asset URLs, so a stale copy of it re-requests the OLD ones and
    the content hash never gets a chance to work. Behind Ingress the panel is
    mounted under an opaque prefix, and how that request arrives is not ours to
    decide, so `handle_index` sets it itself."""
    from petkit_local.web import panel as panel_mod

    app, _reg, _hub = _panel()
    c = await _mk_client(app)
    try:
        r = await c.get("/")
        assert r.headers.get("Cache-Control") == "no-cache"
        body = await r.text()
        # And the document says which build it is, stamped by the same value
        # that versions the asset URLs.
        assert f'window.PANEL_ASSET_V = "{panel_mod.ASSET_VERSION}"' in body
        assert f"app.js?v={panel_mod.ASSET_VERSION}" in body
    finally:
        await c.close()


async def test_a_stale_page_can_tell_that_it_is_stale():
    """`/api/info`'s `version` reports the SERVER's build, which is a different
    question and cannot see this: a fresh server behind a cached page reports
    the new number while running the old code. `asset_version` is the same
    value from two moments — one that arrived with the document, one answered
    live — so a mismatch means the page is old and nothing else can cause it.
    """
    from petkit_local.web import panel as panel_mod

    app, _reg, _hub = _panel()
    c = await _mk_client(app)
    try:
        info = await (await c.get("/api/info")).json()
        assert info["asset_version"] == panel_mod.ASSET_VERSION
        js = await (await c.get("/static/app.js")).text()
        # The comparison, and something visible when it fails — a check whose
        # result goes nowhere is the same as no check.
        assert "PANEL_ASSET_V" in js
        assert "stale-banner" in js
        css = await (await c.get("/static/styles.css")).text()
        assert ".stale-banner" in css
    finally:
        await c.close()


# --- proxy settings: validation and gating ---

def _proxy_settings_app(tmp=None, store=None):
    live = dict.fromkeys(LIVE_SETTINGS, False)
    live["proxy_upstream"] = ""
    cfg = {"api_url": "http://x/6/", "capture": False, "capture_dir": "/nope"}
    if tmp:
        cfg["settings_path"] = str(Path(tmp) / "overrides.json")
    app = create_panel_app(DeviceRegistry(), BLERegistry(), EventHub(), cfg, None,
                           live_config=live, event_store=store)
    return app, live


async def test_settings_expose_every_live_key_with_its_default():
    """All keys always present, so the frontend never renders `undefined`."""
    app, reg, hub = _panel()
    c = await _mk_client(app)
    try:
        s = (await (await c.get("/api/settings")).json())["settings"]
        assert set(s) == set(LIVE_SETTINGS)
        # Both guards default ON — a debugging tool that lets the cloud run a
        # command or push firmware is a liability.
        assert s["proxy_block_run_cmd"] is True
        assert s["proxy_block_ota"] is True
        assert s["proxy_mqtt_bridge"] is True
        assert s["proxy_media_real_oss"] is False
    finally:
        await c.close()


async def test_upstream_presets_are_offered_and_accepted():
    """The choices the panel renders must be exactly what resolve_upstream takes."""
    app, live = _proxy_settings_app()
    c = await _mk_client(app)
    try:
        upstreams = (await (await c.get("/api/info")).json())["upstreams"]
        offered = [u["key"] for u in upstreams]
        assert offered == ["petkit-eu", "petkit-americas", "petkit-asia",
                           "petkit-cn", "petkit-ru"]
        # Exactly one is flagged default, so the picker never has to guess which
        # key an empty setting means.
        assert [u["key"] for u in upstreams if u["default"]] == ["petkit-eu"]

        for key in offered:
            r = await c.post("/api/settings", data=json.dumps({"proxy_upstream": key}))
            assert r.status == 200, key
            assert live["proxy_upstream"] == key
    finally:
        await c.close()


async def test_a_custom_upstream_url_is_accepted():
    app, live = _proxy_settings_app()
    c = await _mk_client(app)
    try:
        r = await c.post("/api/settings", data=json.dumps({"proxy_upstream": "https://my.mirror/6/"}))
        assert r.status == 200
        assert live["proxy_upstream"] == "https://my.mirror/6/"
    finally:
        await c.close()


async def test_a_bad_upstream_is_rejected_and_changes_nothing():
    """Coercion alone would save a typo happily and then fail on every device
    request with nothing in the panel to say why."""
    app, live = _proxy_settings_app()
    c = await _mk_client(app)
    try:
        for bad in ["not a url", "ftp://petkt.com", "https://", "javascript:alert(1)"]:
            r = await c.post("/api/settings", data=json.dumps({"proxy_upstream": bad}))
            assert r.status == 400, bad
            assert live["proxy_upstream"] == "", bad
    finally:
        await c.close()


async def test_a_rejected_batch_applies_none_of_its_settings():
    """Half-applied settings would leave proxy mode on pointing nowhere."""
    app, live = _proxy_settings_app()
    c = await _mk_client(app)
    try:
        r = await c.post("/api/settings", data=json.dumps(
            {"proxy_mode": True, "proxy_upstream": "nonsense"}))
        assert r.status == 400
        assert live["proxy_mode"] is False
    finally:
        await c.close()


# --- /api/blocked ---

async def test_blocked_needs_a_store():
    app, live = _proxy_settings_app()
    c = await _mk_client(app)
    try:
        assert (await c.get("/api/blocked")).status == 400
    finally:
        await c.close()


async def test_blocked_masks_payloads_unless_asked(event_store):
    """These rows hold real device secrets and media keys, and this panel is
    served unauthenticated on the HTTPS port."""
    await event_store.add_blocked_attempts([
        {"device_id": 1, "kind": "secret", "endpoint": "/6/t5/dev_signup",
         "payload_json": "supersecretvalue"},
    ])
    app, live = _proxy_settings_app(store=event_store)
    c = await _mk_client(app)
    try:
        rows = (await (await c.get("/api/blocked")).json())["records"]
        assert rows[0]["payload_json"] == "supers… (16 chars)"

        rows = (await (await c.get("/api/blocked?reveal=1")).json())["records"]
        assert rows[0]["payload_json"] == "supersecretvalue"
    finally:
        await c.close()


async def test_blocked_orders_newest_first_and_clamps_limit(event_store):
    await event_store.add_blocked_attempts(
        [{"device_id": 1, "kind": "rce", "endpoint": f"/6/e{i}"} for i in range(5)])
    app, live = _proxy_settings_app(store=event_store)
    c = await _mk_client(app)
    try:
        rows = (await (await c.get("/api/blocked")).json())["records"]
        assert [r["endpoint"] for r in rows] == [f"/6/e{i}" for i in reversed(range(5))]
        assert len((await (await c.get("/api/blocked?limit=2")).json())["records"]) == 2
        assert len((await (await c.get("/api/blocked?limit=99999")).json())["records"]) == 5
        assert len((await (await c.get("/api/blocked?limit=junk")).json())["records"]) == 5
    finally:
        await c.close()


async def test_blocked_filters_by_device_and_kind(event_store):
    await event_store.add_blocked_attempts([
        {"device_id": 1, "kind": "rce"}, {"device_id": 2, "kind": "ota"},
    ])
    app, live = _proxy_settings_app(store=event_store)
    c = await _mk_client(app)
    try:
        rows = (await (await c.get("/api/blocked?device=2")).json())["records"]
        assert [r["kind"] for r in rows] == ["ota"]
        rows = (await (await c.get("/api/blocked?kind=rce")).json())["records"]
        assert [r["device_id"] for r in rows] == [1]
    finally:
        await c.close()


# --- frontend contract ---
#
# These read the frontend source because there is no JS runtime here to render
# it in. They match with REGEXES rather than literal substrings on purpose: an
# earlier version pinned the exact spelling — `(key,val,title,hint)` with no
# spaces, `${on?'':' disabled'}`, `.k.redact{` with no space before the brace —
# so running a formatter over the panel failed seven assertions without a
# single behaviour having changed. What each test wants to know is a property
# of the rendered UI, so it must survive reformatting, requoting and rewrapping.


def _static(name: str) -> str:
    """One of the panel's static assets, as source text."""
    return (Path(__file__).resolve().parents[1] / "petkit_local" / "web" / "static"
            / name).read_text()


def test_every_hub_kind_the_proxy_publishes_has_a_colour():
    """web/hub.py's kinds are rendered as `.k.<kind>`; one with no CSS rule is
    an unstyled grey blob in the Log tab."""
    css = _static("styles.css")
    for kind in ("redact", "blocked"):
        assert re.search(rf"\.k\.{kind}\s*\{{", css), kind


def test_proxy_dependent_controls_are_gated_on_proxy_mode():
    """Everything that only means something with proxy on must be hidden or
    greyed out when it is off."""
    js = _static("app.js")
    css = _static("styles.css")

    # The guards render through a helper that greys them out with proxy mode.
    assert re.search(r"const guard\s*=\s*\(\s*key\s*,\s*val\s*,", js)
    # ...and that helper appends the greying class inside one interpolation.
    assert re.search(r"ctrl\$\{\s*on\s*\?[^}]*disabled", js)
    assert re.search(r"\.ctrl\.disabled\s*\{", css)
    # The upstream picker and the blocked table are hidden outright.
    assert re.search(r"\$\{\s*on\s*\?(?:.|\n){0,400}?Upstream server", js)
    assert re.search(r"\$\{\s*on\s*\?(?:.|\n){0,200}?blockedView", js)


def test_the_photo_cap_agrees_with_the_store():
    """The panel disables its add-photo tile at MAX_FACES and the store refuses
    past MAX_FACES_PER_PET. Two copies of one number the device enforces for
    real, so a drift means the UI invites an upload the API then rejects."""
    from petkit_local.events.store import MAX_FACES_PER_PET

    m = re.search(r"const MAX_FACES\s*=\s*(\d+)", _static("app.js"))
    assert m, "app.js no longer declares MAX_FACES"
    assert int(m.group(1)) == MAX_FACES_PER_PET


async def test_info_names_the_ai_capable_products():
    """The Pets tab used to spell the product list out in its own copy, which
    went stale the moment a fountain joined it. One source now."""
    app, reg, hub = _panel()
    c = await _mk_client(app)
    try:
        names = (await (await c.get("/api/info")).json())["ai_device_names"]
        assert "Purobot Max Pro 2" in names          # t5
        assert "EverSweet Ultra AI" in names         # w7h, per PetKit's own list
        assert names == sorted(names)
    finally:
        await c.close()


async def test_the_ha_indicator_reports_ha_not_the_device_bridge():
    """`bridge_connected` used to read the DEVICE-facing bridge's client and
    render it as "Bridge -> HA". That bridge is up whenever our own embedded
    broker is, so the one indicator answering "is Home Assistant getting my
    data?" showed green with HA publishing switched off entirely — and would
    have stayed green if the HA broker connection dropped.
    """
    from petkit_local.devices.registry import DeviceRegistry
    reg = DeviceRegistry()
    reg.get_or_create(petkit_id=1, device_type="t5", serial_number="SN")

    class FakePublisher:
        def __init__(self, connected):
            self.connected = connected

    # No publisher at all — running standalone, or with --no-ha. Not an error.
    app, _, _ = _panel(reg=reg)
    c = await _mk_client(app)
    try:
        info = await (await c.get("/api/info")).json()
        assert info["ha_enabled"] is False and info["ha_publishing"] is False
    finally:
        await c.close()

    # Configured but not connected must NOT read the same as connected.
    app, _, _ = _panel(reg=reg)
    app["ha_publisher"] = FakePublisher(False)
    c = await _mk_client(app)
    try:
        info = await (await c.get("/api/info")).json()
        assert info["ha_enabled"] is True and info["ha_publishing"] is False
    finally:
        await c.close()

    app, _, _ = _panel(reg=reg)
    app["ha_publisher"] = FakePublisher(True)
    c = await _mk_client(app)
    try:
        assert (await (await c.get("/api/info")).json())["ha_publishing"] is True
    finally:
        await c.close()


async def test_provision_refuses_a_bluetooth_only_accessory():
    """A W5 or a CTW3 shows up in the chooser because the filter is a name
    prefix, and selecting one used to end in a raw `NotFoundError` about a GATT
    service. There is no WiFi on those models to configure at all."""
    app, reg, hub = _panel()
    c = await _mk_client(app)
    try:
        js = await (await c.get("/static/app.js")).text()
        for prefix in ("Petkit_W5", "Petkit_CTW3", "Petkit_K3"):
            assert prefix in js, prefix
        assert "BLE accessories" in js
    finally:
        await c.close()


async def test_provision_does_not_claim_success_for_a_write_that_returned():
    """"provisioned — device will restart" was printed because an ATT write
    resolved, which is true of a payload the firmware never understood."""
    app, reg, hub = _panel()
    c = await _mk_client(app)
    try:
        js = await (await c.get("/static/app.js")).text()
        assert "the device never answered" in js
        # And the link is no longer torn down before a reply can arrive.
        assert "}, 1500);" not in js
    finally:
        await c.close()


# --- a BLE accessory is its own panel ---------------------------------------

def _paired_ctw3_app():
    """A panel with one D4SH and a CTW3 relayed by it, already reporting.

    The state comes from the real cmd-230 frame in `test_ble_accessories`, run
    through the real decoder, rather than being typed out here: a hand-written
    copy drifts the moment the decoder learns a field, and then this fixture
    reports an entity as unresolvable that a live accessory would fill.
    """
    from petkit_local.devices.ble import BLERegistry, parse_ctw3_ble_response

    from .test_ble_accessories import CTW3_CMD230

    app, reg, hub = _panel()
    parent = reg.get_or_create(petkit_id=10, device_type="d4sh", serial_number="SN10")
    parent.online = True
    parent.mqtt_connected = True
    ble = BLERegistry()
    dev = ble.register(ble_type="ctw3", petkit_id=700, mac="AABBCCDDEEFF",
                       secret="s", interval=240, link_with=10)
    dev.state = parse_ctw3_ble_response(
        {"device": {"mac": "aabbccddeeff"},
         "payload": [{"cmd": 230, "data": CTW3_CMD230}]})
    dev.last_seen = 1785625866
    app["ble_registry"] = ble
    return app, reg, ble


async def test_the_accessory_view_carries_everything_its_panel_needs():
    """It carried three keys — type, mac, id — while its decoded state, its
    entities and its controls existed only in Home Assistant."""
    app, reg, ble = _paired_ctw3_app()
    c = await _mk_client(app)
    try:
        body = await (await c.get("/api/ble")).json()
        a = body["accessories"][0]
        assert a["name"] == "EverSweet Max Cordless"
        assert a["parent_name"] == "YumShare Dual-Hopper"
        assert a["parent_online"] is True
        assert a["last_seen"] == 1785625866
        # Every entity with a path resolves against the accessory's own state
        # document — one whose `value_path` names a section nothing fills reads
        # unknown forever, which is what the device-side guard exists for too.
        # A button has no path: it is an action, not a reading.
        valued = [e for e in a["entities"] if e["value_path"]]
        assert len(valued) == len(a["entities"]) - 1
        for e in valued:
            assert e["value"] is not None, e["key"]
        assert any(e["component"] == "button" for e in a["entities"])
        assert sum(1 for e in a["entities"] if e["settable"]) == 12
    finally:
        await c.close()


async def test_the_panel_can_set_an_accessory_control():
    app, reg, ble = _paired_ctw3_app()

    class _Bridge:
        _client = object()

        def __init__(self):
            self.sent = []

        async def publish_ble_command(self, parent, dev, cmd, payload):
            self.sent.append((parent.petkit_id, dev.petkit_id, cmd, payload))
            return True

    bridge = _Bridge()
    app["bridge"] = bridge
    c = await _mk_client(app)
    try:
        r = await c.post("/api/ble/700/command",
                         json={"entity": "ctw3_mode", "value": "smart"})
        assert r.status == 200, await r.text()
        assert (await r.json())["delivered"] == "ble"
        parent_id, ble_id, cmd, payload = bridge.sent[0]
        assert (parent_id, ble_id, cmd) == (10, 700, 220)
        # Picking a mode means running in it: power on and pump un-paused,
        # which is what PetKit's app sends, rather than either being read back
        # from whatever the last status held.
        assert payload == bytes([1, 1, 2])
        # Optimistic, like a real device's.
        assert ble.get(700).state["states"]["mode"] == 2
    finally:
        await c.close()


async def test_the_panel_can_press_an_accessory_button():
    """A button has no value, and both the coercion and the optimistic
    write-back are shaped around one. Left alone they answered a press with
    400 and then filed its state under the empty string."""
    app, reg, ble = _paired_ctw3_app()

    class _Bridge:
        _client = object()
        sent: list = []

        async def publish_ble_command(self, parent, dev, cmd, payload):
            self.sent.append((cmd, payload))
            return True

    app["bridge"] = _Bridge()
    c = await _mk_client(app)
    try:
        r = await c.post("/api/ble/700/command", json={"entity": "ctw3_reset_filter"})
        assert r.status == 200, await r.text()
        assert _Bridge.sent[-1][0] == 222
        assert "" not in ble.get(700).state["states"]
    finally:
        await c.close()


async def test_an_accessory_write_says_why_it_cannot_be_delivered():
    """A real device off MQTT still has a heartbeat queue; an accessory has
    nothing to queue into, so silence would be a lie."""
    app, reg, ble = _paired_ctw3_app()
    reg.get(10).mqtt_connected = False
    app["bridge"] = type("B", (), {"_client": object()})()
    c = await _mk_client(app)
    try:
        r = await c.post("/api/ble/700/command",
                         json={"entity": "ctw3_power", "value": "OFF"})
        assert r.status == 409
        assert "not on MQTT" in (await r.json())["error"]
    finally:
        await c.close()


async def test_an_accessory_write_is_refused_before_the_first_reading():
    from petkit_local.devices.ble import BLERegistry

    app, reg, hub = _panel()
    parent = reg.get_or_create(petkit_id=10, device_type="d4sh", serial_number="SN10")
    parent.online = parent.mqtt_connected = True
    ble = BLERegistry()
    ble.register(ble_type="ctw3", petkit_id=700, mac="AABBCCDDEEFF", link_with=10)
    app["ble_registry"] = ble
    app["bridge"] = type("B", (), {"_client": object()})()
    c = await _mk_client(app)
    try:
        r = await c.post("/api/ble/700/command",
                         json={"entity": "ctw3_brightness", "value": "low"})
        assert r.status == 400
        assert "status" in (await r.json())["error"]
        r = await c.post("/api/ble/999/command", json={"entity": "x", "value": "1"})
        assert r.status == 404
    finally:
        await c.close()


async def test_the_accessory_panel_is_its_own_panel_and_is_trimmed():
    app, reg, hub = _panel()
    c = await _mk_client(app)
    try:
        js = await (await c.get("/static/app.js")).text()
        assert "accpanel" in js and "accSummary" in js and "accBody" in js
        assert "relayed by" in js
        # A transport badge that is true of an accessory. `linkBadge`'s two
        # answers are both false: no MQTT session, no heartbeat queue.
        assert "badge ble" in js
        css = await (await c.get("/static/styles.css")).text()
        assert ".badge.ble" in css
        # The parent's machinery must not follow it across.
        body = js[js.index("function accBody("):]
        body = body[: body.index("\n}\n")]
        for absent in ("http_count", "mqtt_count", "queue", "patcher", "log_upload"):
            assert absent not in body, absent
    finally:
        await c.close()


async def test_the_panel_can_ask_for_a_reading_now():
    """The one action an accessory has, and it belongs to the relay: nothing in
    the CTW3 protocol is shaped like "do X now". Without it the only way to
    find out whether a freshly paired accessory answers is to wait out its poll
    interval — up to four minutes of staring at "never"."""
    app, reg, ble = _paired_ctw3_app()

    class _Bridge:
        _client = object()
        asked = None

        async def request_ble_reading(self, parent, dev):
            _Bridge.asked = (parent.petkit_id, dev.petkit_id)
            return True

    app["bridge"] = _Bridge()
    c = await _mk_client(app)
    try:
        r = await c.post("/api/ble/700/poll")
        assert r.status == 200
        assert _Bridge.asked == (10, 700)
        # And it refuses in the same words the write path does.
        reg.get(10).mqtt_connected = False
        r = await c.post("/api/ble/700/poll")
        assert r.status == 409
    finally:
        await c.close()
