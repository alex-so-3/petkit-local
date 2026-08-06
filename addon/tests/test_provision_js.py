"""The provisioning decoders in `web/static/app.js`, run for real.

Everything else about the panel's JavaScript is tested by asserting on its
source text, which catches a deleted feature and nothing else. These functions
are the exception worth the machinery: they decode a binary protocol off real
hardware, three device reports disagree about the details, and a wrong offset
here reads as "your device never answered" rather than as an error.

So the pure helpers are lifted out of `app.js` — they touch no DOM — and
exercised in node against the exact frames the reports describe. Skipped where
node is absent; CI has it, because the prettier check runs through `npx`.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parent.parent / "petkit_local" / "web" / "static" / "app.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node is needed to run the panel's JavaScript")

#: Lifted from app.js by name. All pure, all DOM-free.
FUNCTIONS = ("pkCrc16", "pkFrame", "pkParse", "pkBytes", "pkJoined", "pkJoinFailed", "pkJoinWarn",
             "pkJoinState", "blufiExplain", "provisionUrlWarning")
CONSTANTS = ("PK_MAGIC", "PK_TAIL", "PK_TYPE_OUT", "PK_JOIN_STATES", "PK_JOIN_DONE", "PK_JOIN_FAILED",
             "PK_JOIN_WARN", "BLUFI_TYPE_DATA", "BLUFI_DATA_CUSTOM", "BLUFI_FC_FRAG")


def _extract(src: str, name: str) -> str:
    """One top-level `function name(...) {...}`, by matching its braces.

    A regex cannot do this — the bodies contain braces — and importing app.js
    whole is not an option, since the rest of it reaches for `document` on load.
    """
    start = src.index(f"function {name}(")
    i = src.index("{", start)
    depth = 0
    while True:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
        if depth == 0:
            return src[start:i]


def _harness(body: str) -> str:
    """The extracted helpers, plus a script that exercises them."""
    src = APP_JS.read_text()
    out = []
    for const in CONSTANTS:
        line = next(ln for ln in src.splitlines() if ln.startswith(f"const {const} = "))
        # A multi-line constant (the join-state table) runs to its closing brace.
        if line.rstrip().endswith(("{", "[")):
            idx = src.index(line)
            end = src.index("\n};\n" if line.rstrip().endswith("{") else "\n];\n", idx)
            line = src[idx:end + 3]
        out.append(line)
    out += [_extract(src, fn) for fn in FUNCTIONS]
    out.append(body)
    return "\n".join(out)


def _run(body: str) -> dict:
    """Run `body` against the extracted helpers; it prints one JSON object."""
    proc = subprocess.run(["node", "--input-type=module", "-e", _harness(body)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


#: `{"key":151,"payload":{"state":1}}` — the ack every model sends for the
#: credentials, and the shortest document worth reassembling.
ACK_JS = """
const json = new TextEncoder().encode('{"key":151,"payload":{"state":1}}');
const crc = pkCrc16(json);
const framed = n => new Uint8Array([0xfa, 0xfc, 0xfd, 0x46, 0x13, 0,
  n & 0xff, (n >> 8) & 0xff, ...json, crc & 0xff, (crc >> 8) & 0xff, 0xfb]);
const blufiPkt = (sub, fc, data) =>
  new Uint8Array([1 | (sub << 2), fc, 0, data.length, ...data]);
"""


def test_the_crc_is_the_one_the_devices_use():
    """CRC-16/CCITT-FALSE, whose published check value over "123456789" is
    0x29B1. A CRC that is merely plausible produces frames a device drops in
    silence, which is indistinguishable from every other failure here."""
    out = _run("""
      console.log(JSON.stringify({check: pkCrc16(new TextEncoder().encode('123456789'))}));
    """)
    assert out["check"] == 0x29B1


def test_both_readings_of_the_length_field_are_accepted():
    """The two hardware reports disagree: a T6's framed replies count the JSON
    alone (issue #9), a D4H's count the JSON plus its two CRC bytes (#11).
    Neither can be assumed, so both endings are tried."""
    out = _run(ACK_JS + """
      console.log(JSON.stringify({
        with_crc: pkParse(framed(json.length + 2)).payload.state,
        without:  pkParse(framed(json.length)).payload.state,
        bare:     pkParse(json).key,
        garbage:  pkParse(new Uint8Array([1, 2, 3])),
      }));
    """)
    assert out == {"with_crc": 1, "without": 1, "bare": 151, "garbage": None}


def test_a_notification_is_read_from_its_own_offset():
    """`view.buffer` is the whole underlying ArrayBuffer, which may be longer
    than the view and start before it. Reading it whole works on Chrome today
    and is the kind of thing that stops working on one platform with no way to
    see why."""
    out = _run(ACK_JS + """
      const body = framed(json.length + 2);
      const big = new Uint8Array(64);
      big.set(body, 8);
      const v = new DataView(big.buffer, 8, body.length);
      console.log(JSON.stringify({state: pkParse(v).payload.state}));
    """)
    assert out["state"] == 1


def test_an_outbound_frame_uses_ingenic_crc_length():
    out = _run("""
      const frame = pkFrame(0, {key: 110});
      console.log(JSON.stringify({
        key: pkParse(frame).key,
        type: frame[4],
        seq: frame[5],
        len: frame[6] | (frame[7] << 8),
      }));
    """)
    assert out["key"] == 110
    assert out["type"] == 0x18
    assert out["seq"] == 0
    assert out["len"] == len('{"key":110}') + 2


def test_a_fragmented_esp32_custom_data_reply_is_reassembled():
    """Issue #5, in one assertion. A Pura Max answered three times with
    `type 1 subtype 0x13` — ESP32 custom data, the channel PetKit's own
    document rides on — and the panel logged the subtype number, kept none of
    it, and told the owner the device had never answered.

    Each fragment carries two bytes of remaining-length before its content, so
    a fragment is not JSON on its own and the pieces mean nothing apart.
    """
    out = _run(ACK_JS + """
      const ctx = {frag: [], replies: {}};
      const parts = [json.slice(0, 12), json.slice(12, 24), json.slice(24)];
      let line;
      parts.forEach((p, i) => {
        const last = i === parts.length - 1;
        const data = last
          ? p
          : new Uint8Array([json.length & 0xff, (json.length >> 8) & 0xff, ...p]);
        line = blufiExplain(blufiPkt(0x13, last ? 0x00 : 0x10, data), ctx);
      });
      console.log(JSON.stringify({state: (ctx.replies[151] || {}).state, line}));
    """)
    assert out["state"] == 1
    assert out["line"].startswith("key 151")


def test_server_connected_and_online_count_as_joined():
    """State 7 and 10 are successful. State 9 is MQTT progress and keeps polling."""
    out = _run("""
      console.log(JSON.stringify({
        s7: pkJoined({state: 7}), s10: pkJoined({state: 10}),
        s6: pkJoined({state: 6}), none: pkJoined(undefined),
        unreported: pkJoinState(undefined), named: pkJoinState({state: 6}),
      }));
    """)
    assert out["s7"] and out["s10"]
    assert not out["s6"] and not out["none"]
    assert out["unreported"] == "never reported"
    assert "connecting to the server" in out["named"]


def test_wrong_password_state_is_a_warning_not_terminal():
    """An ESP32 capture reported state 3/code 2 at 16:04:18, then recovered and
    reached server setup with unchanged credentials in the same session."""
    out = _run("""
      console.log(JSON.stringify({
        failed: pkJoinFailed({state: 3, code: 2}),
        warned: pkJoinWarn({state: 3, code: 2}),
        named: pkJoinState({state: 3, code: 2}),
        failedStates: PK_JOIN_FAILED,
      }));
    """)
    assert not out["failed"]
    assert out["warned"]
    assert out["named"] == "the Wi-Fi password is wrong (code 2)"
    assert 3 not in out["failedStates"]


def test_a_reported_failure_is_named_rather_than_numbered():
    """The four failure states came out of PetKit's own app, which logs one
    line per state. Without them a wrong Wi-Fi password rendered as "state 3"
    and the panel went on polling for another twenty-four seconds."""
    out = _run("""
      console.log(JSON.stringify({
        pwd: [pkJoinWarn({state: 3}), pkJoinState({state: 3})],
        missing: [pkJoinFailed({state: 4}), pkJoinState({state: 4})],
        wifi: [pkJoinFailed({state: 5}), pkJoinState({state: 5})],
        server: [pkJoinFailed({state: 8}), pkJoinState({state: 8})],
        ok7: pkJoinFailed({state: 7}), ok9: pkJoinFailed({state: 9}),
        none: pkJoinFailed(undefined),
      }));
    """)
    assert out["pwd"] == [True, "the Wi-Fi password is wrong"]
    assert out["missing"] == [True, "that Wi-Fi network was not found"]
    assert out["wifi"][0] and out["server"][0]
    assert "could not connect to the server" in out["server"][1]
    # 9 is "connecting to MQTT" — progress, not a verdict.
    assert not out["ok7"] and not out["ok9"] and not out["none"]


def test_esp32_provisioning_warns_when_api_url_is_not_port_80():
    out = _run("""
      console.log(JSON.stringify({
        port80: provisionUrlWarning('http://192.168.50.29/6/').join('\\n'),
        port8080: provisionUrlWarning('http://192.168.50.29:8080/6/').join('\\n'),
        https: provisionUrlWarning('https://ha.example/6/').join('\\n'),
      }));
    """)
    assert out["port80"] == ""
    assert "port <b>80</b>" in out["port8080"]
    assert "port <b>80</b>" in out["https"]
