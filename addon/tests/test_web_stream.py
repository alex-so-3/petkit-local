"""The live-view plumbing: the go2rtc stream proxy and the TURN mint."""
import json

import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from petkit_local.devices.registry import DeviceRegistry
from petkit_local.media import turn
from petkit_local.web.api import stream
from petkit_local.web.hub import EventHub
from petkit_local.web.panel import create_panel_app


def _panel_app(tmp_path):
    cfg = {"api_url": "http://x/6/", "capture": False, "capture_dir": "/nope",
           "data_dir": str(tmp_path), "media_root": str(tmp_path / "media")}
    return create_panel_app(DeviceRegistry(), None, EventHub(), cfg)


async def _client(app):
    c = TestClient(TestServer(app))
    await c.start_server()
    return c


async def _fake_go2rtc(monkeypatch):
    """A stand-in go2rtc: an echo WebSocket on `/api/ws`, a frame on
    `/api/frame.jpeg`, and a config surface that must never be reachable."""
    async def ws_echo(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_str(json.dumps({"src": request.query.get("src", "")}))
        async for m in ws:
            if m.type == WSMsgType.TEXT:
                await ws.send_str(m.data)
            elif m.type == WSMsgType.BINARY:
                await ws.send_bytes(m.data)
        return ws

    async def frame(request):
        return web.Response(body=b"\xff\xd8jpeg", content_type="image/jpeg")

    async def config(request):
        return web.json_response({"secret": "config surface"})

    app = web.Application()
    app.router.add_get("/api/ws", ws_echo)
    app.router.add_get("/api/frame.jpeg", frame)
    app.router.add_route("*", "/api/config", config)
    client = TestClient(TestServer(app))
    await client.start_server()
    monkeypatch.setattr(stream, "GO2RTC_API_ADDR", f"127.0.0.1:{client.server.port}")
    return client


# --- the whitelist ------------------------------------------------------------

async def test_the_config_surface_is_not_reachable_through_the_proxy(tmp_path, monkeypatch):
    """The whole point of proxying instead of exposing go2rtc: its API can add
    and rewrite streams, so only the streaming endpoints pass."""
    upstream = await _fake_go2rtc(monkeypatch)
    client = await _client(_panel_app(tmp_path))
    try:
        for path in ("api/config", "api/streams", "api/restart", "", "api"):
            resp = await client.get(f"/api/stream/{path}")
            assert resp.status == 403, path
    finally:
        await client.close()
        await upstream.close()


async def test_an_unreachable_go2rtc_is_a_502_not_a_hang(tmp_path, monkeypatch):
    monkeypatch.setattr(stream, "GO2RTC_API_ADDR", "127.0.0.1:1")
    client = await _client(_panel_app(tmp_path))
    try:
        resp = await client.get("/api/stream/api/frame.jpeg?src=1")
        assert resp.status == 502
    finally:
        await client.close()


async def test_a_ws_upgrade_to_a_dead_go2rtc_fails_the_handshake(tmp_path, monkeypatch):
    """go2rtc is dialled BEFORE the browser's upgrade is accepted, so a dead
    upstream answers the handshake with a 502 the frontend can distinguish
    from a live stream that ended. Accept-then-close diagnoses as nothing."""
    import aiohttp
    monkeypatch.setattr(stream, "GO2RTC_API_ADDR", "127.0.0.1:1")
    client = await _client(_panel_app(tmp_path))
    try:
        with pytest.raises(aiohttp.WSServerHandshakeError) as exc:
            await client.ws_connect("/api/stream/api/ws?src=1")
        assert exc.value.status == 502
    finally:
        await client.close()


# --- pass-through --------------------------------------------------------------

async def test_a_frame_passes_through_with_its_type_and_query(tmp_path, monkeypatch):
    upstream = await _fake_go2rtc(monkeypatch)
    client = await _client(_panel_app(tmp_path))
    try:
        resp = await client.get("/api/stream/api/frame.jpeg?src=1")
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("image/jpeg")
        assert await resp.read() == b"\xff\xd8jpeg"
    finally:
        await client.close()
        await upstream.close()


async def test_the_websocket_is_bridged_in_both_directions(tmp_path, monkeypatch):
    """MSE segments come down and WebRTC offers go up on the same socket, so
    the bridge has to pump both ways — and text and binary alike."""
    upstream = await _fake_go2rtc(monkeypatch)
    client = await _client(_panel_app(tmp_path))
    try:
        ws = await client.ws_connect("/api/stream/api/ws?src=42")
        greeting = json.loads((await ws.receive()).data)
        assert greeting == {"src": "42"}, "query string did not reach go2rtc"
        await ws.send_str("hello")
        assert (await ws.receive()).data == "hello"
        await ws.send_bytes(b"\x00\x01segment")
        assert (await ws.receive()).data == b"\x00\x01segment"
        await ws.close()
    finally:
        await client.close()
        await upstream.close()


# --- TURN -----------------------------------------------------------------------

async def test_no_turn_key_is_an_empty_list_not_an_error(tmp_path):
    client = await _client(_panel_app(tmp_path))
    try:
        resp = await client.get("/api/turn")
        assert resp.status == 200
        assert await resp.json() == {"iceServers": []}
    finally:
        await client.close()


async def test_minted_credentials_are_handed_through(tmp_path, monkeypatch):
    """With a key configured, /api/turn answers Cloudflare's ICE object —
    and sends the API token only upstream, never back to the browser."""
    seen = {}

    async def mint(request):
        seen["auth"] = request.headers.get("Authorization")
        seen["ttl"] = (await request.json()).get("ttl")
        return web.json_response({"iceServers": {
            "urls": ["turn:turn.cloudflare.com:3478?transport=udp"],
            "username": "u", "credential": "c"}})

    cf = web.Application()
    cf.router.add_post("/keys/{key}/credentials/generate", mint)
    cf_client = TestClient(TestServer(cf))
    await cf_client.start_server()
    monkeypatch.setattr(
        turn, "CF_ENDPOINT",
        f"http://127.0.0.1:{cf_client.server.port}/keys/{{key}}/credentials/generate")
    (tmp_path / "turn.json").write_text(
        json.dumps({"key_id": "k1", "api_token": "tok"}))

    client = await _client(_panel_app(tmp_path))
    try:
        body = await (await client.get("/api/turn")).json()
        assert body["iceServers"][0]["username"] == "u"
        assert "tok" not in json.dumps(body)
        assert seen["auth"] == "Bearer tok"
        assert seen["ttl"] == 3600
    finally:
        await client.close()
        await cf_client.close()


async def test_a_failed_mint_degrades_to_no_ice_servers(tmp_path, monkeypatch):
    async def refuse(request):
        return web.Response(status=403, text="bad key")

    cf = web.Application()
    cf.router.add_post("/keys/{key}/credentials/generate", refuse)
    cf_client = TestClient(TestServer(cf))
    await cf_client.start_server()
    monkeypatch.setattr(
        turn, "CF_ENDPOINT",
        f"http://127.0.0.1:{cf_client.server.port}/keys/{{key}}/credentials/generate")
    (tmp_path / "turn.json").write_text(
        json.dumps({"key_id": "k1", "api_token": "tok"}))
    try:
        assert await turn.cloudflare_ice_servers(str(tmp_path)) is None
    finally:
        await cf_client.close()


def test_a_malformed_or_missing_key_file_reads_as_unconfigured(tmp_path):
    assert turn.turn_configured(str(tmp_path)) is False
    (tmp_path / "turn.json").write_text("{not json")
    assert turn.turn_configured(str(tmp_path)) is False
    (tmp_path / "turn.json").write_text(json.dumps({"key_id": "k"}))  # no token
    assert turn.turn_configured(str(tmp_path)) is False
    (tmp_path / "turn.json").write_text(
        json.dumps({"key_id": "k", "api_token": "t"}))
    assert turn.turn_configured(str(tmp_path)) is True
