"""Live view: the go2rtc stream proxy and the TURN credential mint.

go2rtc's API listens on loopback only (`media/go2rtc.py::API_ADDR`) — correctly,
because that API can add and rewrite streams. But its MSE WebSocket and WebRTC
signalling live on the same listener, so a browser has no way to reach a live
view at all. `api_stream_proxy` forwards exactly the streaming subset of that
API — never the config surface — on the panel's own origin, which also keeps it
working behind Home Assistant Ingress and any TLS the operator puts in front.

`api_turn` is the companion for watching from OFF the LAN: it mints short-lived
Cloudflare TURN credentials (see `media/turn.py`) that a frontend passes to its
`RTCPeerConnection`, giving WebRTC a relay candidate when go2rtc's LAN candidate
is unreachable. Unconfigured, it answers an empty list and costs nothing.
"""
from __future__ import annotations

import asyncio
import contextlib

import aiohttp
from aiohttp import web

from petkit_local.media.go2rtc import API_ADDR as GO2RTC_API_ADDR
from petkit_local.media.turn import cloudflare_ice_servers

#: go2rtc endpoints the browser is allowed to reach through us: the signalling
#: WebSocket (MSE + WebRTC ride the same socket) and the WHEP-style offer.
STREAM_PROXY_EXACT = ("api/ws", "api/webrtc")
#: ...plus the single-frame and muxed-stream families, which carry a container
#: extension (`api/frame.jpeg`, `api/stream.mp4`). The prefixes end at the dot
#: ON PURPOSE: a bare `api/stream` prefix would also match `api/streams` —
#: go2rtc's stream CONFIG endpoint, whose PUT registers an arbitrary source
#: (including `exec:`) — and the whole point of proxying instead of exposing
#: the port is that the config surface stays unreachable.
STREAM_PROXY_PREFIXES = ("api/frame.", "api/stream.")

#: One streamed chunk. Small enough to keep latency low on a live stream, big
#: enough that a 1080p MSE segment does not cost hundreds of syscalls.
_CHUNK = 64 * 1024


def _allowed(tail: str) -> bool:
    """Whether `tail` names a whitelisted go2rtc streaming endpoint."""
    return tail in STREAM_PROXY_EXACT or tail.startswith(STREAM_PROXY_PREFIXES)


async def _pump_ws(src: web.WebSocketResponse | aiohttp.ClientWebSocketResponse,
                   dst: web.WebSocketResponse | aiohttp.ClientWebSocketResponse) -> None:
    """Copy WebSocket messages from `src` to `dst` until either side ends."""
    async for m in src:
        if m.type == aiohttp.WSMsgType.TEXT:
            await dst.send_str(m.data)
        elif m.type == aiohttp.WSMsgType.BINARY:
            await dst.send_bytes(m.data)
        elif m.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
            break


async def api_stream_proxy(request: web.Request) -> web.StreamResponse:
    """Same-origin reverse proxy to the whitelisted go2rtc streaming endpoints.

    The WebSocket (MSE segments and WebRTC signalling ride the same socket) is
    bridged in both directions; whichever side closes first tears the bridge
    down rather than leaving the other pump parked on a silent socket. Plain
    HTTP is streamed through chunk by chunk — `api/stream.mp4` never ends, so
    buffering a whole upstream response here would grow without bound.
    """
    tail = request.match_info["path"]
    if not _allowed(tail):
        return web.json_response({"error": "not a permitted stream path"}, status=403)
    qs = request.query_string
    base = f"{GO2RTC_API_ADDR}/{tail}" + (f"?{qs}" if qs else "")

    if request.headers.get("Upgrade", "").lower() == "websocket":
        # Dial go2rtc BEFORE accepting the browser's upgrade: a dead upstream
        # then answers the handshake itself with a 502 the frontend can tell
        # apart from a live stream that ended. Accept-then-close reads as
        # "opened fine, closed at once", which diagnoses as nothing.
        session = aiohttp.ClientSession()
        try:
            client_ws = await session.ws_connect(f"ws://{base}")
        except (aiohttp.ClientError, ConnectionError):
            await session.close()
            return web.json_response({"error": "go2rtc unreachable"}, status=502)
        server_ws = web.WebSocketResponse()
        await server_ws.prepare(request)
        try:
            down = asyncio.create_task(_pump_ws(client_ws, server_ws))
            up = asyncio.create_task(_pump_ws(server_ws, client_ws))
            try:
                await asyncio.wait({down, up}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in (down, up):
                    t.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await t
        finally:
            with contextlib.suppress(Exception):
                await client_ws.close()
            await session.close()
            with contextlib.suppress(Exception):
                await server_ws.close()
        return server_ws

    # Plain HTTP: a JPEG frame, or an endless muxed stream — so stream it.
    try:
        async with aiohttp.ClientSession() as session:
            async with session.request(
                request.method, f"http://{base}",
                data=await request.read() if request.body_exists else None,
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=5),
            ) as upstream:
                response = web.StreamResponse(status=upstream.status)
                ctype = upstream.headers.get("Content-Type")
                if ctype:
                    response.content_type = ctype.split(";")[0]
                await response.prepare(request)
                try:
                    async for chunk in upstream.content.iter_chunked(_CHUNK):
                        await response.write(chunk)
                except (aiohttp.ClientError, ConnectionResetError):
                    pass  # viewer left or upstream ended; either way, done
                with contextlib.suppress(Exception):
                    await response.write_eof()
                return response
    except (aiohttp.ClientError, ConnectionError, asyncio.TimeoutError):
        return web.json_response({"error": "go2rtc unreachable"}, status=502)


async def api_turn(request: web.Request) -> web.Response:
    """Short-lived TURN ICE servers for the live view, `[]` when unconfigured.

    The frontend passes these to its `RTCPeerConnection` so WebRTC can relay
    through Cloudflare when the browser is off the LAN (go2rtc's host candidate
    is unreachable then). No `turn.json` → `{"iceServers": []}` and the player
    proceeds LAN-direct or falls back to MSE; the API token itself never leaves
    the server — only the minted, expiring username/credential do.
    """
    data_dir = request.app["cfg"].get("data_dir", "/data")
    ice = await cloudflare_ice_servers(data_dir, ttl=3600)
    return web.json_response({"iceServers": [ice] if ice else []})
