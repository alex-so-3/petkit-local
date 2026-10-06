"""A refusal that an MQTT 5.0 client can actually read.

T6 firmware 951 opens with a v5 CONNECT. amqtt speaks 3.1.1 only and answers
with a 3.1.1 CONNACK, which a v5 client cannot parse -- v5 adds a properties
field after the reason code -- so the device never sees the refusal and waits
out its own connect timeout instead.

Measured on the wire against a real T6 on 951: our refusal cost **10.2 s**,
while PetKit's own cloud refuses the same client in 0.7 s. The device walks its
server list one entry per 30 s and only one of those entries is us, so those
ten seconds are most of the window in which the box has no MQTT session at all
and its commands fall back to the HTTP heartbeat queue.

This does NOT implement MQTT 5. It refuses it *properly*, with a v5 CONNACK
carrying reason code 0x84 (Unsupported Protocol Version), which is what lets
the client's own fallback to 3.1.1 fire at once instead of after a timeout.

Monkey-patching amqtt is the established shape here: `broker.py` already
replaces `_create_ssl_context` to match the device's mbedtls cipher list.
"""
from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Any

from amqtt.mqtt.connack import UNACCEPTABLE_PROTOCOL_VERSION, ConnackPacket
from amqtt.mqtt.connect import ConnectPacket
from amqtt.mqtt.protocol import broker_handler

log = logging.getLogger(__name__)

#: MQTT 5 CONNACK, whole: fixed header 0x20, remaining length 3, acknowledge
#: flags 0x00, reason code 0x84 (Unsupported Protocol Version), property
#: length 0. The property length is the byte a 3.1.1 CONNACK does not carry and
#: whose absence is exactly what the v5 client chokes on.
_V5_REFUSAL = b"\x20\x03\x00\x84\x00"

MQTT5_PROTOCOL_LEVEL = 5

#: Protocol level of the CONNECT currently being handled. Each client is served
#: by its own asyncio task, so a ContextVar keeps concurrent connections apart
#: where a module global would not.
_proto_level: ContextVar[int | None] = ContextVar(
    "petkit_connect_proto_level", default=None,
)

_installed = False


class _V5RefusalConnack(ConnackPacket):
    """A CONNACK serialised in MQTT 5 shape, for refusing a v5 CONNECT.

    Only `to_bytes` differs: staying a ConnackPacket keeps it valid for the
    PACKET_SENT plugin event that amqtt fires before writing it.
    """

    def to_bytes(self) -> bytes:
        return _V5_REFUSAL


class _VersionAwareConnackPacket(ConnackPacket):
    """`build()` that answers a v5 CONNECT in v5 shape when refusing it.

    Narrow on purpose: only the unsupported-version refusal is rewritten, and
    only when the CONNECT we are answering really was v5. Every other CONNACK,
    including the refusal sent to a 3.1 client, is built by amqtt unchanged.
    """

    @classmethod
    def build(cls, *args: Any, **kwargs: Any) -> ConnackPacket:
        return_code = args[1] if len(args) > 1 else kwargs.get("return_code")
        if (
            return_code == UNACCEPTABLE_PROTOCOL_VERSION
            and _proto_level.get() == MQTT5_PROTOCOL_LEVEL
        ):
            log.info("Refusing an MQTT 5.0 CONNECT with a v5 CONNACK (reason 0x84)")
            return _V5RefusalConnack()
        return ConnackPacket.build(*args, **kwargs)


def install() -> None:
    """Patch amqtt in place. Idempotent, so a second call is harmless."""
    global _installed
    if _installed:
        return

    _orig_from_stream = ConnectPacket.from_stream

    async def _from_stream(reader: Any, fixed_header: Any = None,
                           variable_header: Any = None) -> Any:
        packet = await _orig_from_stream(reader, fixed_header, variable_header)
        # A malformed CONNECT is amqtt's to reject; we only note the version,
        # and a missing one must leave the stock path untouched.
        _proto_level.set(getattr(packet, "proto_level", None))
        return packet

    ConnectPacket.from_stream = _from_stream  # type: ignore[assignment]
    broker_handler.ConnackPacket = _VersionAwareConnackPacket  # type: ignore[assignment]
    _installed = True
    log.info("MQTT 5.0 CONNECTs will now be refused in a shape their client can read")
