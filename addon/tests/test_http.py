"""End-to-end dry test of the HTTP API using aiohttp's in-process test client.

Exercises the real routing + middleware + handlers + registry without a device
or any network — the whole boot sequence a PetKit device performs.
"""
import json
import os
import tempfile
import time

from aiohttp.test_utils import TestClient, TestServer

from petkit_local.devices.registry import DeviceRegistry
from petkit_local.http.handlers.heartbeat import IOT_STATUS_GRACE
from petkit_local.http.server import create_app
from petkit_local.patchers.common import cleanup_staged, stage_file

CONFIG = {
    "api_url": "http://server/6/",
    "mqtt_port": 1883,
    "proxy_mode": False,
    "proxy_upstream": "",
    "proxy_block_run_cmd": True,
}

HDR = {"X-Device": "id=100&sn=SN100"}

# A device (or anything on the LAN) is free to send a non-numeric id. Every
# handler used to feed it to a bare int(), which answered HTTP 500.
HDR_BAD_ID = {"X-Device": "id=abc&sn=SN-NOT-REGISTERED"}
HDR_BAD_ID_KNOWN_SN = {"X-Device": "id=%2e%2e%2f&sn=SN100"}


async def _client(registry, config=None):
    app = create_app(registry, config or CONFIG)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def test_full_boot_sequence():
    reg = DeviceRegistry()
    client = await _client(reg)
    try:
        # syncTime
        r = await client.post("/6/t5/dev_syncTime", headers=HDR)
        assert r.status == 200
        assert isinstance((await r.json())["result"], int)

        # signup
        r = await client.post("/6/t5/dev_signup", headers=HDR)
        assert r.status == 200
        res = (await r.json())["result"]
        assert res["id"] == 100
        assert res["sn"] == "SN100"
        assert res["secret"]
        # firmware is strict: signupAt STRING, createdAt NUMBER, both present
        assert isinstance(res["signupAt"], str)
        assert isinstance(res["createdAt"], int)
        assert reg.get(100) is not None

        # iot device info -> MQTT credentials. First fetch is optimistic: hand
        # our own broker host (derived from api_url) to try real MQTT.
        r = await client.post("/6/t5/dev_only_iot_device_info_v2", headers=HDR)
        ali = (await r.json())["result"]["ali"]
        assert ali["mqttHost"] == "server"
        assert ali["productKey"] and ali["deviceName"] and ali["deviceSecret"]
        # credentials match what MQTT auth will validate against
        dev = reg.get(100)
        assert ali["productKey"] == dev.mqtt_product_key
        assert ali["deviceSecret"] == dev.mqtt_device_secret

        # serverinfo -> our own URL
        r = await client.post("/6/t5/dev_serverinfo", headers=HDR)
        assert (await r.json())["result"]["apiServers"] == ["http://server/6/"]

        # state report -> parsed into device.state
        body = {"workState": 1, "sandPercent": 55, "usedTimes": 9}
        r = await client.post("/6/t5/dev_state_report", headers=HDR, data=json.dumps(body))
        assert r.status == 200
        assert "interval" in (await r.json())["result"]
        assert dev.state["workingState"] == 1
        assert dev.state["sandPercent"] == 55

        # heartbeat with no commands
        r = await client.get("/6/poll/t5/heartbeat", headers=HDR)
        result = (await r.json())["result"]
        assert isinstance(result, list)
    finally:
        await client.close()


# --- a device that puts its identity in the body ----------------------------
#
# The Ingenic models send `X-Device: id=...&sn=...` on every request. An ESP32
# feeder sends no such header, no query string either, and urlencodes
# everything into the POST body. Until 1.5.0 that device was unidentifiable to
# every endpoint here.

# The signup a Feeder D4 running firmware 1.267 actually sends, from the
# capture in issue #3. Kept whole rather than trimmed to the fields under test:
# the point is that this exact string works.
D4_SIGNUP_BODY = (
    "hardware=1&firmware=1.267&mac=c05d89d25204&timezone=2.0"
    "&locale=Europe/Amsterdam&id=400090690&sn=20241223G11497"
    "&bt_mac=c05d89d25206&ap_mac=c05d89d25205&chipid=13783556"
)
FORM = {"Content-Type": "application/x-www-form-urlencoded"}


async def test_a_device_that_identifies_itself_only_in_the_body_registers():
    """This answered 400 "missing device id" — the device never got past its
    first request, and no amount of retrying would have helped."""
    reg = DeviceRegistry()
    client = await _client(reg)
    try:
        r = await client.post("/6/d4/dev_signup", data=D4_SIGNUP_BODY, headers=FORM)
        assert r.status == 200
        dev = reg.get(400090690)
        assert dev is not None
        assert dev.device_type == "d4"
        # Not just the id: a blank serial would have burnt itself into
        # `mqtt_device_name`, which is minted once and never repaired.
        assert dev.serial_number == "20241223G11497"
        assert dev.mqtt_device_name == "d_d4_20241223G11497"
        assert dev.mac == "c05d89d25204"
        assert dev.firmware == "1.267"
        # The device is the only source of its own BLE address and offers it
        # exactly once, here.
        assert dev.config.get("bt_mac") == "c05d89d25206"
    finally:
        await client.close()


async def test_a_device_is_told_back_the_timezone_it_reported():
    """Its signup body carries `timezone` and `locale` — both are in the T4's
    own format string in firmware, and the captured D4 body has them — and both
    were read off the wire and dropped. The reply then used the SERVER's offset,
    so a device that had just said it was at -4.0 was answered 2.0, and one that
    said `en-US` was answered with nothing.

    The device is the authority on where it is: it was handed that value over
    BLE at provisioning and burns it into its video watermarks.
    """
    reg = DeviceRegistry()
    client = await _client(reg)
    try:
        body = D4_SIGNUP_BODY.replace("timezone=2.0", "timezone=-4.0") \
                             .replace("locale=Europe/Amsterdam", "locale=en-US")
        res = (await (await client.post("/6/d4/dev_signup", data=body,
                                        headers=FORM)).json())["result"]
        assert res["timezone"] == -4.0
        assert res["locale"] == "en-US"

        # A manual override is for an install whose box lives elsewhere, so it
        # has to keep winning over anything a device says about itself.
        reg.get(400090690).config["timezone"] = 5.5
        res = (await (await client.post("/6/d4/dev_signup", data=body,
                                        headers=FORM)).json())["result"]
        assert res["timezone"] == 5.5
    finally:
        await client.close()


async def test_a_timezone_that_is_not_one_is_not_stored():
    """A device reporting a parse failure as 0 is indistinguishable from one
    genuinely at UTC, so only a value inside the range an offset can occupy is
    believed; anything else leaves the field to the server's own clock."""
    reg = DeviceRegistry()
    client = await _client(reg)
    try:
        for bad in ("timezone=99", "timezone=-40.5", "timezone=abc", "timezone="):
            body = D4_SIGNUP_BODY.replace("timezone=2.0", bad)
            await client.post("/6/d4/dev_signup", data=body, headers=FORM)
            assert "reported_timezone" not in reg.get(400090690).config, bad
    finally:
        await client.close()


async def test_the_same_device_then_gets_its_mqtt_credentials():
    """The quieter half of the same bug: `dev_iot_device_info` answered 200
    with an empty result, so the device never reached the broker and nothing
    anywhere looked wrong."""
    reg = DeviceRegistry()
    client = await _client(reg)
    try:
        await client.post("/6/d4/dev_signup", data=D4_SIGNUP_BODY, headers=FORM)
        r = await client.post("/6/d4/dev_iot_device_info",
                              data="id=400090690&sn=20241223G11497", headers=FORM)
        result = (await r.json())["result"]
        assert result, "no credentials at all"
        assert result.get("deviceSecret") or result.get("productKey")
    finally:
        await client.close()


async def test_the_header_still_wins_over_the_body():
    """Precedence is header, then query, then body. A device sending both is
    not something anybody has seen, but the order has to be decided."""
    reg = DeviceRegistry()
    client = await _client(reg)
    try:
        await client.post("/6/t5/dev_signup", headers={**HDR, **FORM},
                          data="id=999&sn=SN999")
        assert reg.get(100) is not None
        assert reg.get(999) is None
    finally:
        await client.close()


async def test_a_body_with_no_usable_id_still_answers_400():
    """The body is a third source, not an excuse to accept anything."""
    reg = DeviceRegistry()
    client = await _client(reg)
    try:
        r = await client.post("/6/d4/dev_signup", data="sn=SN&id=abc", headers=FORM)
        assert r.status == 400
        assert (await r.json())["error"] == "missing device id"
    finally:
        await client.close()


async def test_heartbeat_delivers_queued_command():
    reg = DeviceRegistry()
    client = await _client(reg)
    try:
        await client.post("/6/t5/dev_signup", headers=HDR)
        dev = reg.get(100)
        dev.command_queue.append({"msgType": 2, "payload": {"start_action": 0}})

        r = await client.get("/6/poll/t5/heartbeat", headers=HDR)
        result = (await r.json())["result"]
        assert len(result) == 1
        content = json.loads(result[0]["content"])
        assert content["payload"]["start_action"] == 0
        # queue drained after delivery
        assert dev.command_queue == []
    finally:
        await client.close()


# --- what the device does with the answer ----------------------------------
#
# Every rule below was read out of `net_http_heartbeat` (D4SH 867) and its ARM
# twin (W7H 456), and every one of them fails silently: a rejected entry
# produces no reply, no error and no retry, so from this side a command the
# device threw away is indistinguishable from one it obeyed.


def test_a_command_is_labelled_with_a_msg_type_the_device_dispatches():
    """`do_http_web_msg` knows 0, 1 and 2. Anything else logs
    `//***** error msgType (%d) *****//` and the command is gone.

    This table read 2/3/5/6/7 until 1.4.0 — invented and sequential. `start`
    worked by coincidence; every settings write and every feed sent over HTTP
    was discarded by the device while the queue drained and
    `wait_for_heartbeat` reported delivery.
    """
    from petkit_local.ha.commands import ALL_ACTIONS
    from petkit_local.http.handlers.heartbeat import _to_heartbeat_content

    for key, build in ALL_ACTIONS.items():
        suffix, envelope = build()
        content = json.loads(_to_heartbeat_content(
            {**envelope, "_service_suffix": suffix}))
        assert content["msgType"] in (0, 1, 2), f"{key} -> {content['msgType']}"


def test_a_settings_write_goes_to_the_property_set_handler():
    from petkit_local.ha.commands import make_mqtt_property_set
    from petkit_local.http.handlers.heartbeat import _to_heartbeat_content

    content = json.loads(_to_heartbeat_content(
        make_mqtt_property_set({"petDetection": 1})))
    assert content["msgType"] == 1
    # `web_property_set_recv_parse` reads `payload` and nothing else.
    assert content["payload"] == {"petDetection": 1}
    # And no `type`, because the real cloud sends none. Captured from PetKit's
    # own servers setting a D4's indicator light (PR #10). The key is inert
    # either way, but a body that matches theirs is one a capture of ours can
    # be compared against.
    assert "type" not in content


def test_a_service_keeps_the_name_the_firmware_dispatches_on():
    """`parse_service_invoke_msg` takes the service from `type` verbatim when
    `service_id` is absent, which is exactly the HTTP case."""
    from petkit_local.http.handlers.heartbeat import _to_heartbeat_content

    for method, expected in [("thing.service.start", "start"),
                             ("thing.service.end", "end"),
                             ("thing.service.feed_realtime", "feed_realtime"),
                             ("thing.service.connect", "connect")]:
        content = json.loads(_to_heartbeat_content(
            {"method": method, "params": {"x": 1}}))
        assert content["msgType"] == 2, method
        assert content["type"] == expected, method


async def test_two_commands_in_one_answer_both_survive_the_device_s_filters():
    """The device drops an entry whose `timestamp` is not greater than the last
    one it consumed, and one whose `time/1000 - timestamp` reaches 61 — on an
    unsigned compare, so a stamp in the future expires instantly. Identical
    stamps satisfied the second rule and failed the first, delivering only the
    head of a batch."""
    reg = DeviceRegistry()
    client = await _client(reg)
    try:
        await client.post("/6/t5/dev_signup", headers=HDR)
        dev = reg.get(100)
        for _ in range(3):
            dev.command_queue.append({"msgType": 2, "payload": {"start_action": 0}})

        result = (await (await client.get("/6/poll/t5/heartbeat",
                                          headers=HDR)).json())["result"]
        assert len(result) == 3
        stamps = [e["timestamp"] for e in result]
        assert stamps == sorted(set(stamps)), "stamps must strictly increase"
        for entry in result:
            age = entry["time"] // 1000 - entry["timestamp"]
            assert 0 <= age < 61, entry
    finally:
        await client.close()


async def test_heartbeat_clears_mqtt_flag_when_device_reports_no_session():
    """`mqtt_connected` is set by a successful CONNECT and used to pick a
    transport; publishing to a topic nobody is subscribed to raises nothing, so
    a flag left up after the session died swallowed every command silently."""
    reg = DeviceRegistry()
    client = await _client(reg)
    try:
        await client.post("/6/t5/dev_signup", headers=HDR)
        dev = reg.get(100)
        dev.mqtt_connected = True

        await client.get("/6/poll/t5/heartbeat?iotStatus=0", headers=HDR)
        assert dev.mqtt_connected is False
    finally:
        await client.close()


async def test_heartbeat_ignores_an_iot_status_that_lags_the_connect():
    """The device samples iotStatus before it sends the poll, so a heartbeat
    already in flight when the session came up reports the state from just
    before it — 112ms after the CONNECT on the reference T5. Acting on that
    zero would cancel the connect that beat it."""
    reg = DeviceRegistry()
    client = await _client(reg)
    try:
        await client.post("/6/t5/dev_signup", headers=HDR)
        dev = reg.get(100)
        dev.mqtt_connected = True
        dev.mqtt_connected_at = time.time()

        await client.get("/6/poll/t5/heartbeat?iotStatus=0", headers=HDR)
        assert dev.mqtt_connected is True

        # Once the session is older than the grace window the same report is
        # evidence of a real loss.
        dev.mqtt_connected_at = time.time() - (IOT_STATUS_GRACE + 1)
        await client.get("/6/poll/t5/heartbeat?iotStatus=0", headers=HDR)
        assert dev.mqtt_connected is False
    finally:
        await client.close()


async def test_heartbeat_never_sets_the_mqtt_flag():
    """iotStatus=1 says the device is on *a* broker, not on ours — in proxy
    mode that may be Aliyun's. Only an authenticated CONNECT proves the
    difference, so this half of the signal is one-way."""
    reg = DeviceRegistry()
    client = await _client(reg)
    try:
        await client.post("/6/t5/dev_signup", headers=HDR)
        dev = reg.get(100)
        assert dev.mqtt_connected is False

        await client.get("/6/poll/t5/heartbeat?iotStatus=1", headers=HDR)
        assert dev.mqtt_connected is False
    finally:
        await client.close()


async def test_heartbeat_keeps_mqtt_flag_without_a_readable_status():
    """No news is not bad news: a heartbeat with no iotStatus, or one carrying
    something we cannot read, leaves the flag exactly as it was."""
    reg = DeviceRegistry()
    client = await _client(reg)
    try:
        await client.post("/6/t5/dev_signup", headers=HDR)
        dev = reg.get(100)

        for query in ("", "?iotStatus=", "?iotStatus=maybe"):
            dev.mqtt_connected = True
            await client.get(f"/6/poll/t5/heartbeat{query}", headers=HDR)
            assert dev.mqtt_connected is True, query
    finally:
        await client.close()


async def test_heartbeat_still_delivers_commands_while_clearing_the_flag():
    """The clearing runs before `pop_commands`, which is destructive and
    at-most-once — it must not cost the reply its command."""
    reg = DeviceRegistry()
    client = await _client(reg)
    try:
        await client.post("/6/t5/dev_signup", headers=HDR)
        dev = reg.get(100)
        dev.mqtt_connected = True
        dev.command_queue.append({"msgType": 2, "payload": {"start_action": 0}})

        r = await client.get("/6/poll/t5/heartbeat?iotStatus=0", headers=HDR)
        result = (await r.json())["result"]
        assert len(result) == 1
        assert json.loads(result[0]["content"])["payload"]["start_action"] == 0
        assert dev.mqtt_connected is False
    finally:
        await client.close()


async def test_esp32_iot_device_info_is_flat():
    reg = DeviceRegistry()
    client = await _client(reg)
    try:
        await client.post("/6/t4/dev_signup", headers=HDR)
        # ESP32 endpoint -> flat block, no `ali` wrapper
        r = await client.post("/6/t4/dev_iot_device_info", headers=HDR)
        res = (await r.json())["result"]
        assert "ali" not in res
        assert res["iotPlatform"] == "ALI"
        assert res["productKey"] and res["deviceSecret"] and res["mqttHost"]
        # Ingenic endpoint -> ali-wrapped
        r2 = await client.post("/6/t4/dev_only_iot_device_info_v2", headers=HDR)
        assert "ali" in (await r2.json())["result"]
    finally:
        await client.close()


async def test_mqtt_host_is_always_our_broker():
    # No global mqtt_host setting and no HTTP-only fallback: every device is
    # handed our own broker host (derived from api_url), every time.
    reg = DeviceRegistry()
    client = await _client(reg)
    try:
        await client.post("/6/t5/dev_signup", headers=HDR)
        for _ in range(3):
            r = await client.post("/6/t5/dev_only_iot_device_info_v2", headers=HDR)
            assert (await r.json())["result"]["ali"]["mqttHost"] == "server"
    finally:
        await client.close()


async def test_serverinfo_shape():
    reg = DeviceRegistry()
    client = await _client(reg)
    try:
        await client.post("/6/t5/dev_signup", headers=HDR)
        r = await client.post("/6/t5/dev_serverinfo", headers=HDR)
        res = (await r.json())["result"]
        assert res["dns"] == ""               # empty string (proven T5 format)
        assert res["nextTick"] == 3600  # the real cloud's value
        assert res["apiServers"][0].endswith("/6/")
    finally:
        await client.close()


async def test_serverinfo_is_the_same_shape_before_and_after_signup():
    """The unregistered path used to keep its own copy of the literal, so a
    change to `to_serverinfo` silently applied to only half the callers."""
    reg = DeviceRegistry()
    client = await _client(reg)
    try:
        unknown = (await (await client.post("/6/t5/dev_serverinfo",
                                            headers=HDR_BAD_ID)).json())["result"]
        await client.post("/6/t5/dev_signup", headers=HDR)
        known = (await (await client.post("/6/t5/dev_serverinfo",
                                          headers=HDR)).json())["result"]
        assert set(unknown) == set(known)
        assert unknown["nextTick"] == known["nextTick"] == 3600
    finally:
        await client.close()


async def test_signup_carries_the_fields_the_real_cloud_sends():
    """Additive against the capture. `signupAt`/`createdAt` stay even though
    the cloud omits them — they are the documented boot-loop guard, and the
    capture is of an already-registered device so it cannot speak to signup."""
    reg = DeviceRegistry()
    client = await _client(reg)
    try:
        res = (await (await client.post("/6/t5/dev_signup", headers=HDR)).json())["result"]
        assert isinstance(res["signupAt"], str) and isinstance(res["createdAt"], int)
        for key in ("petInTipLimit", "p2pType", "tooManyPets",
                    "frequencyPetTip", "deodorantTip", "purificationTip"):
            assert key in res, key
    finally:
        await client.close()


async def test_upload_log_is_acknowledged_the_way_the_cloud_does():
    """A bare string result, not the catch-all's object. Nothing is uploaded —
    we never issue a token — but the shape is what the firmware expects."""
    reg = DeviceRegistry()
    client = await _client(reg)
    try:
        await client.post("/6/t5/dev_signup", headers=HDR)
        assert (await (await client.post("/6/t5/dev_upload_log",
                                         headers=HDR)).json()) == {"result": "success"}
        assert (await (await client.post("/6/t5/dev_upload_log_token",
                                         headers=HDR)).json()) == {"result": {}}
    finally:
        await client.close()


async def test_multi_config_json_strings():
    reg = DeviceRegistry()
    client = await _client(reg)
    try:
        await client.post("/6/t5/dev_signup", headers=HDR)
        r = await client.post("/6/t5/dev_multi_config", headers=HDR)
        res = (await r.json())["result"]
        assert isinstance(res["lightMultiRange"], str)
        assert json.loads(res["lightMultiRange"]) == {"lightMultiRange": [[0, 1440]]}
        cam = json.loads(res["cameraMultiRange"])["cameraMultiRange"]
        assert cam[0]["enable"] == 1
        assert cam[0]["time"] == [[0, 1440]]
    finally:
        await client.close()


async def test_ota_check_is_an_empty_object():
    reg = DeviceRegistry()
    client = await _client(reg)
    try:
        r = await client.post("/6/t5/dev_ota_check", headers=HDR)
        assert (await r.json())["result"] == {}
    finally:
        await client.close()


async def test_unknown_endpoint_returns_empty_result():
    reg = DeviceRegistry()
    client = await _client(reg)
    try:
        r = await client.post("/6/t5/dev_some_unknown_thing", headers=HDR)
        assert r.status == 200
        assert (await r.json()) == {"result": {}}
    finally:
        await client.close()


async def test_malformed_device_id_never_returns_500():
    # One request per handler that used to do `int(x_dev["id"])`. The point is
    # the status code: anything but a 5xx means the coercion degraded instead
    # of aborting the request.
    endpoints = [
        ("post", "/6/t5/dev_serverinfo"),
        ("post", "/6/t5/dev_state_report"),
        ("post", "/6/t5/dev_only_iot_device_info_v2"),
        ("post", "/6/t5/dev_iot_device_info"),
        ("post", "/6/t5/dev_ble_device"),
        ("post", "/6/t5/dev_schedule_get"),
        ("post", "/6/t5/dev_feed_get"),
        ("post", "/6/t5/dev_device_info"),
        ("post", "/6/t5/dev_multi_config"),
        ("post", "/6/t5/dev_oss_sts_info_new_v2"),
        ("post", "/6/t5/dev_video_device_info"),
        ("post", "/6/t5/dev_discern_pic"),
        ("post", "/6/t5/dev_discern_config"),
        ("post", "/6/t5/dev_event_report"),
        ("post", "/6/t5/dev_upload_file_info_v2"),
        ("get", "/6/poll/t5/heartbeat"),
    ]
    reg = DeviceRegistry()
    with tempfile.TemporaryDirectory() as tmp:
        # dev_oss_sts_info_new_v2 persists the bucket AES key under data_dir.
        client = await _client(reg, dict(CONFIG, data_dir=tmp))
        try:
            for method, path in endpoints:
                r = await getattr(client, method)(path, headers=HDR_BAD_ID)
                assert r.status == 200, f"{path} -> {r.status}"
                assert isinstance(await r.json(), dict)
            # Signup is the one endpoint that answers with an error status: an
            # unusable id is the same as a missing one, not a crash.
            r = await client.post("/6/t5/dev_signup", headers=HDR_BAD_ID)
            assert r.status == 400
            assert (await r.json())["error"] == "missing device id"

            # Resolution must never mint a device — only signup/iot_device_info do.
            assert reg.all() == []
        finally:
            await client.close()


async def test_malformed_device_id_falls_back_to_serial():
    reg = DeviceRegistry()
    client = await _client(reg)
    try:
        await client.post("/6/t5/dev_signup", headers=HDR)
        dev = reg.get(100)
        dev.command_queue.append({"msgType": 2, "payload": {"start_action": 0}})

        # Unusable id + a known serial -> the same device, with its real
        # command queue and its real MQTT credentials.
        r = await client.get("/6/poll/t5/heartbeat", headers=HDR_BAD_ID_KNOWN_SN)
        result = (await r.json())["result"]
        assert json.loads(result[0]["content"])["payload"]["start_action"] == 0

        r = await client.post("/6/t5/dev_only_iot_device_info_v2", headers=HDR_BAD_ID_KNOWN_SN)
        assert (await r.json())["result"]["ali"]["deviceSecret"] == dev.mqtt_device_secret
        # …and still no second device got created for the bogus id.
        assert len(reg.all()) == 1
    finally:
        await client.close()


async def test_signup_still_accepts_the_id_from_the_query_string():
    # Some firmware signs up with ?id=&sn= and no X-Device header at all.
    reg = DeviceRegistry()
    client = await _client(reg)
    try:
        r = await client.post("/6/t5/dev_signup?id=101&sn=SN101&firmware=943")
        assert r.status == 200
        res = (await r.json())["result"]
        assert res["id"] == 101 and res["sn"] == "SN101"
        assert reg.get(101).firmware == "943"
    finally:
        await client.close()


async def test_signup_rejects_a_non_positive_id():
    # id=0 means "unidentified", not "device zero" — the registry drops
    # petkit_id <= 0 as a phantom on reload.
    reg = DeviceRegistry()
    client = await _client(reg)
    try:
        for query in ("?id=0&sn=SN0", "?id=-5&sn=SN0"):
            r = await client.post("/6/t5/dev_signup" + query)
            assert r.status == 400
        assert reg.all() == []
    finally:
        await client.close()


async def test_schedule_echoes_the_resolved_device_id():
    reg = DeviceRegistry()
    client = await _client(reg)
    try:
        await client.post("/6/t5/dev_signup", headers=HDR)
        r = await client.post("/6/t5/dev_schedule_get", headers=HDR)
        rows = (await r.json())["result"]
        assert all(row["deviceId"] == 100 for row in rows)
        # Distinct ids, as the real cloud sends — every row shared `id: 0`
        # before, which is not an id at all.
        assert len({row["id"] for row in rows}) == len(rows)
        # An ISO-8601 string, not a unix int, and FIXED: re-stamping it with
        # now() claimed the schedule had changed on every poll.
        assert rows[0]["updatedAt"].endswith("+0000")
        again = (await (await client.post("/6/t5/dev_schedule_get", headers=HDR)).json())["result"]
        assert again == rows

        # Unidentified requester keeps the 0 the firmware has always received.
        r = await client.post("/6/t5/dev_schedule_get", headers=HDR_BAD_ID)
        assert all(row["deviceId"] == 0 for row in (await r.json())["result"])
    finally:
        await client.close()


async def test_patcher_download_serves_a_staged_file():
    reg = DeviceRegistry()
    client = await _client(reg)
    try:
        stage_file("test_http_staged.bin", b"patched-binary")
        r = await client.get("/patcher/download/test_http_staged.bin")
        assert r.status == 200
        assert await r.read() == b"patched-binary"
    finally:
        cleanup_staged("test_http_staged.bin")
        await client.close()


async def test_patcher_download_rejects_traversal():
    reg = DeviceRegistry()
    client = await _client(reg)
    try:
        for path in ("/patcher/download/..%2F..%2Fetc%2Fpasswd",
                     "/patcher/download/..%5C..%5Cetc%5Cpasswd",
                     "/patcher/download/%2Fetc%2Fpasswd"):
            r = await client.get(path)
            assert r.status in (400, 404), f"{path} -> {r.status}"
            assert b"root:" not in await r.read()
    finally:
        await client.close()


async def test_patcher_download_rejects_a_symlink_out_of_the_stage_dir():
    # The old check only looked for "/" and ".." in the name, so a symlink
    # inside the staging dir was served whatever it pointed at.
    from petkit_local.patchers.common import STAGE_DIR

    reg = DeviceRegistry()
    client = await _client(reg)
    link = os.path.join(STAGE_DIR, "test_http_escape.bin")
    try:
        os.makedirs(STAGE_DIR, exist_ok=True)
        if os.path.lexists(link):
            os.unlink(link)
        os.symlink("/etc/hosts", link)
        r = await client.get("/patcher/download/test_http_escape.bin")
        assert r.status == 400
    finally:
        if os.path.lexists(link):
            os.unlink(link)
        await client.close()


async def test_patcher_download_404_for_missing_file():
    reg = DeviceRegistry()
    client = await _client(reg)
    try:
        r = await client.get("/patcher/download/no_such_staged_file.bin")
        assert r.status == 404
    finally:
        await client.close()


async def test_index_lists_devices():
    reg = DeviceRegistry()
    client = await _client(reg)
    try:
        await client.post("/6/t5/dev_signup", headers=HDR)
        r = await client.get("/")
        data = await r.json()
        assert data["service"] == "petkit-local"
        assert any(d["id"] == 100 for d in data["devices"])
    finally:
        await client.close()
