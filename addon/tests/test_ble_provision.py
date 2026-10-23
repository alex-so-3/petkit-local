"""ble_provision.py against the Provision tab it transcribes.

Every vector below was produced by running web/static/js/provision.js itself
(pkCrc16, pkFrame, blufiSend) under Node, not written by hand. A failure here
means the Python has drifted from the page: fix the Python, or — if the page
changed on purpose — regenerate the vectors from the page.
"""
import json

from petkit_local import ble_provision as bp

JS = json.loads(r'''{"crc":[12852,65535],"frame110":"fafcfd4618000d007b226b6579223a3131307d3432fb","frame151":"fafcfd461801bd007b226b6579223a3135312c227061796c6f6164223a7b2273736964223a224d7957696669222c22707764223a227365637265742070617373222c2268696465223a312c226c6f63616c65223a224575726f70652f526f6d65222c2274696d657a6f6e65223a22312e30222c2261706953657276657273223a5b22687474703a2f2f3139322e3136382e312e31302f362f225d2c22697053657276657273223a5b22687474703a2f2f3139322e3136382e312e31302f362f225d7d7da00cfb","frame_seq300":"fafcfd46182c0d007b226b6579223a3131327d5654fb","blufi151":["4d10000ebb007b226b6579223a3135312c22","4d10010eaf007061796c6f6164223a7b2273","4d10020ea300736964223a224d7957696669","4d10030e9700222c22707764223a22736563","4d10040e8b007265742070617373222c2268","4d10050e7f00696465223a312c226c6f6361","4d10060e73006c65223a224575726f70652f","4d10070e6700526f6d65222c2274696d657a","4d10080e5b006f6e65223a22312e30222c22","4d10090e4f0061706953657276657273223a","4d100a0e43005b22687474703a2f2f313932","4d100b0e37002e3136382e312e31302f362f","4d100c0e2b00225d2c226970536572766572","4d100d0e1f0073223a5b22687474703a2f2f","4d100e0e13003139322e3136382e312e3130","4d000f072f362f225d7d7d"],"blufi110_seq5":["4d00050b7b226b6579223a3131307d"]}''')

CFG = {
    "ssid": "MyWifi", "pwd": "secret pass", "hide": 1, "locale": "Europe/Rome",
    "timezone": "1.0", "apiServers": ["http://192.168.1.10/6/"],
    "ipServers": ["http://192.168.1.10/6/"],
}


def test_crc_matches_page():
    assert bp.pk_crc16(b'{"key":110}') == JS["crc"][0]
    assert bp.pk_crc16(b"") == JS["crc"][1]


def test_frames_match_page_byte_for_byte():
    assert bp.pk_frame(0, {"key": 110}).hex() == JS["frame110"]
    assert bp.pk_frame(1, {"key": 151, "payload": CFG}).hex() == JS["frame151"]


def test_sequence_wraps_at_one_byte_like_the_page():
    assert bp.pk_frame(300, {"key": 112}).hex() == JS["frame_seq300"]


def test_blufi_fragments_match_page():
    frames, nxt = bp.blufi_frames(0, bp._json({"key": 151, "payload": CFG}))
    assert [f.hex() for f in frames] == JS["blufi151"]
    assert nxt == len(JS["blufi151"])


def test_blufi_short_message_is_one_unfragmented_frame():
    frames, nxt = bp.blufi_frames(5, bp._json({"key": 110}))
    assert [f.hex() for f in frames] == JS["blufi110_seq5"]
    assert nxt == 6


def test_payload_is_the_page_payload_in_the_page_order():
    got = bp.build_payload("MyWifi", "secret pass", "http://192.168.1.10/6/", 1, "Europe/Rome")
    assert got == CFG
    assert list(got) == list(CFG)


def test_timezone_rounds_like_number_tofixed():
    # Values from (h).toFixed(1) in Node. Python's own format() would give
    # "0.2" for 0.25 (half-to-even); the page sends "0.3".
    cases = {5.75: "5.8", 12.75: "12.8", 8.75: "8.8", -9.5: "-9.5", 1: "1.0",
             -3.25: "-3.3", 0.25: "0.3", 5.5: "5.5"}
    for hours, text in cases.items():
        assert bp.timezone_text(hours) == text, hours


def test_parse_reads_our_own_frames_back():
    assert bp.pk_parse(bp.pk_frame(7, {"key": 112, "payload": {"state": 10}})) == {
        "key": 112, "payload": {"state": 10}}


def test_parse_accepts_a_real_t5_identity_reply_as_bare_json():
    # Shape of the key-110 answer a T5 (fw 943) sent during provisioning.
    raw = b'{"key":110,"payload":{"id":1,"sn":"X","mac":"00","hardware":1,"version":"943"}}'
    assert bp.pk_parse(raw)["payload"]["version"] == "943"


def test_parse_never_raises_on_garbage():
    for junk in (b"", b"\xfa\xfc\xfd\x46\x13\x00\xff\xff\xff\xff\xff", b"\xff\xfe", b"{"):
        assert bp.pk_parse(junk) is None


def test_join_state_text():
    assert bp.join_state_text({"state": 10, "code": 0}) == "online (code 0)"
    assert bp.join_state_text(None) == "never reported"
    assert bp.join_state_text({"state": 99}) == "state 99"


def test_password_never_logged():
    shown = bp._redacted({"key": 151, "payload": dict(CFG)})
    assert shown["payload"]["pwd"] == "***"
    assert CFG["pwd"] == "secret pass"


def test_module_imports_without_bleak():
    # CI imports every module and the add-on image has no Bluetooth stack, so
    # bleak may only be imported inside run(). A None entry in sys.modules makes
    # `import bleak` raise ImportError, as on a host without it.
    import subprocess
    import sys
    code = ("import sys; sys.modules['bleak'] = None; "
            "import petkit_local.ble_provision as m; print(m.BLE_SERVICE)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
