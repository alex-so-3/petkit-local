"""Cloudflare TURN credentials for WebRTC that works off the LAN.

go2rtc's only good ICE candidate here is its LAN address (see
`go2rtc.py::lan_ip`), which reaches a browser on the same LAN and nothing else.
To watch from the public internet — say behind a Cloudflare tunnel, which
carries the HTTP/WS signalling but NOT the media UDP — the peers need a relay.
Cloudflare's TURN service relays without any inbound port: you mint short-lived
credentials against a TURN key, and the browser dials out to the relay.

Split of duties (the browser side lives in whatever frontend consumes
`web/api/stream.py`; go2rtc's side is rendered by `go2rtc.py::render_config`):
  · the BROWSER gets TURN here (a relay candidate the far side can always reach);
  · go2rtc gets a static STUN server (a srflx candidate — its home public IP).
TURN permissions are keyed on the peer IP, so that pair relays even through a
symmetric home NAT, and go2rtc never needs rotating credentials of its own.

Config lives in ``{data_dir}/turn.json`` as ``{"key_id": ..., "api_token": ...}``
(created by the operator with a Cloudflare TURN key). Absent or malformed → this
returns None and remote WebRTC is simply unavailable; LAN WebRTC and the MSE
fallback are unaffected. The API token never leaves this process: only the
minted, expiring username/credential are handed to the browser.
"""
from __future__ import annotations

import json
import logging
import os

import aiohttp

log = logging.getLogger(__name__)

#: Cloudflare's mint-credentials endpoint for a TURN key. Module-level so a
#: test can point it at a local server.
CF_ENDPOINT = "https://rtc.live.cloudflare.com/v1/turn/keys/{key}/credentials/generate"
#: STUN the go2rtc side uses to learn its reflexive address. Public, no auth,
#: no rotation — a plain URL in the go2rtc config is enough.
STUN_URL = "stun:stun.cloudflare.com:3478"


def _creds_path(data_dir: str) -> str:
    return os.path.join(data_dir, "turn.json")


def _read_key(data_dir: str) -> tuple[str, str] | None:
    """(`key_id`, `api_token`) from turn.json, or None if not configured."""
    try:
        with open(_creds_path(data_dir), encoding="utf-8") as f:
            cfg = json.load(f)
        key_id, token = cfg.get("key_id"), cfg.get("api_token")
        return (key_id, token) if key_id and token else None
    except (OSError, ValueError):
        return None


def turn_configured(data_dir: str) -> bool:
    """Whether a Cloudflare TURN key is present to mint from."""
    return _read_key(data_dir) is not None


async def cloudflare_ice_servers(data_dir: str, ttl: int = 3600) -> dict | None:
    """Mint one `{urls, username, credential}` ICE-server object, or None.

    `ttl` is the credential lifetime in seconds. Never raises — a missing key,
    a network error or a non-2xx reply all degrade to None (no remote WebRTC),
    which the caller reports as an empty ICE list.
    """
    key = _read_key(data_dir)
    if key is None:
        return None
    key_id, token = key
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                CF_ENDPOINT.format(key=key_id),
                headers={"Authorization": f"Bearer {token}"},
                json={"ttl": ttl},
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status not in (200, 201):
                    body = (await resp.text())[:200]
                    log.warning("Cloudflare TURN mint failed: HTTP %d %s",
                                resp.status, body)
                    return None
                data = await resp.json()
                # Cloudflare returns {"iceServers": {"urls": [...], "username":
                # ..., "credential": ...}} — a single object, which the browser
                # wraps in its iceServers list.
                return data.get("iceServers")
    except (aiohttp.ClientError, OSError, ValueError) as e:
        log.warning("Cloudflare TURN mint error: %s", e)
        return None
