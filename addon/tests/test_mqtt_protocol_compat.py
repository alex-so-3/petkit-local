"""Tests for petkit_local/mqtt/protocol_compat.py — refusing MQTT 5.0 in v5 shape.

Driven through amqtt's own packet classes and the very name `broker_handler`
calls (`ConnackPacket.build`), with no broker and no socket. What that proves is
the narrow rewrite: a v5 CONNECT refused for its version gets the 5-byte v5
CONNACK, and every other CONNACK stays amqtt's. Whether a given firmware's
client then falls back to 3.1.1 at once is a property of the device, measured
on hardware, not here.
"""
import asyncio
import contextvars

from amqtt.adapters import BufferReader
from amqtt.mqtt.connack import (
    BAD_USERNAME_PASSWORD,
    UNACCEPTABLE_PROTOCOL_VERSION,
    ConnackPacket,
)
from amqtt.mqtt.connect import ConnectPacket
from amqtt.mqtt.protocol import broker_handler

from petkit_local.mqtt import protocol_compat

V5_REFUSAL = b"\x20\x03\x00\x84\x00"


def _connect(level: int) -> bytes:
    """A minimal clean-session CONNECT with client id `a` at `level`.

    A v5 CONNECT carries a property length (here 0) after the keep-alive, which
    is the byte a 3.1.1 parser does not expect.
    """
    variable = b"\x00\x04MQTT" + bytes([level]) + b"\x02\x00\x3c"
    if level == protocol_compat.MQTT5_PROTOCOL_LEVEL:
        variable += b"\x00"
    payload = b"\x00\x01a"
    body = variable + payload
    return b"\x10" + bytes([len(body)]) + body


async def _refusal_after(level: int, return_code: int) -> bytes:
    await ConnectPacket.from_stream(BufferReader(_connect(level)))
    return broker_handler.ConnackPacket.build(0, return_code).to_bytes()


def _in_fresh_context(coro_fn, *args):
    """Run each case as its own client task would: in a context of its own."""
    ctx = contextvars.copy_context()
    return ctx.run(lambda: asyncio.run(coro_fn(*args)))


def test_install_is_idempotent():
    protocol_compat.install()
    patched = broker_handler.ConnackPacket
    protocol_compat.install()
    assert broker_handler.ConnackPacket is patched


def test_v5_connect_refused_for_version_gets_v5_connack():
    protocol_compat.install()
    out = _in_fresh_context(_refusal_after, 5, UNACCEPTABLE_PROTOCOL_VERSION)
    assert out == V5_REFUSAL


def test_v31_connect_refused_for_version_keeps_311_connack():
    protocol_compat.install()
    out = _in_fresh_context(_refusal_after, 3, UNACCEPTABLE_PROTOCOL_VERSION)
    assert out == ConnackPacket.build(0, UNACCEPTABLE_PROTOCOL_VERSION).to_bytes()
    assert out != V5_REFUSAL


def test_v5_connect_other_refusal_is_not_rewritten():
    protocol_compat.install()
    out = _in_fresh_context(_refusal_after, 5, BAD_USERNAME_PASSWORD)
    assert out == ConnackPacket.build(0, BAD_USERNAME_PASSWORD).to_bytes()


def test_no_connect_seen_leaves_build_stock():
    protocol_compat.install()

    def build() -> bytes:
        return broker_handler.ConnackPacket.build(
            0, UNACCEPTABLE_PROTOCOL_VERSION,
        ).to_bytes()

    out = contextvars.Context().run(build)
    assert out == ConnackPacket.build(0, UNACCEPTABLE_PROTOCOL_VERSION).to_bytes()


def test_v5_refusal_is_still_a_connack():
    """amqtt fires PACKET_SENT with it before writing, so it must stay one."""
    protocol_compat.install()

    async def build() -> object:
        await ConnectPacket.from_stream(BufferReader(_connect(5)))
        return broker_handler.ConnackPacket.build(0, UNACCEPTABLE_PROTOCOL_VERSION)

    packet = _in_fresh_context(build)
    assert isinstance(packet, ConnackPacket)
