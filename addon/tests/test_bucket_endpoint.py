"""Where the device is told to upload — derived, never assumed.

Reported by a user running docker-compose: every upload URL in the STS response
read `https://localhost:9000`, which on the device resolves to the device. The
add-on path had always been fine because the Supervisor hands over a host IP;
standalone had nothing and fell back to a constant.
"""
import json
import subprocess
import sys

from petkit_local.config import Config
from petkit_local.devices.base import Device


def test_it_follows_the_address_the_device_was_given():
    """`api_url` is the one address known to work: the device is configured to
    call it, and the request being answered arrived on it."""
    c = Config(api_url="http://192.0.2.55:8080/6/", bucket_port=9000)
    c.resolve_bucket_endpoint()
    assert c.bucket_endpoint == "https://192.0.2.55:9000"


def test_it_uses_the_configured_bucket_port():
    c = Config(api_url="http://192.0.2.55:8080/6/", bucket_port=9443)
    c.resolve_bucket_endpoint()
    assert c.bucket_endpoint == "https://192.0.2.55:9443"


def test_a_hostname_works_as_well_as_an_ip():
    c = Config(api_url="http://petkit.lan:8080/6/")
    c.resolve_bucket_endpoint()
    assert c.bucket_endpoint == "https://petkit.lan:9000"


def test_an_explicit_endpoint_wins():
    """The add-on path sets this from the Supervisor's host IP before we get
    here, and must not be second-guessed."""
    c = Config(api_url="http://192.0.2.55:8080/6/",
               bucket_endpoint="https://10.0.0.9:9000")
    c.resolve_bucket_endpoint()
    assert c.bucket_endpoint == "https://10.0.0.9:9000"


def test_the_cli_exposes_bucket_endpoint():
    proc = subprocess.run(
        [sys.executable, "-m", "petkit_local.main", "--help"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert proc.returncode == 0
    assert "--bucket-endpoint" in proc.stdout


def test_no_api_url_leaves_it_empty_rather_than_inventing_one():
    c = Config(api_url="")
    c.resolve_bucket_endpoint()
    assert c.bucket_endpoint == ""


def test_the_sts_response_never_says_localhost():
    """The actual bug. `localhost` is not merely useless to the device — it is
    the device, so the upload 'succeeds' at nothing or fails forever."""
    d = Device(device_type="t5", petkit_id=1, serial_number="SN")
    body = json.dumps(d.to_oss_sts("", "0123456789abcdef"))
    assert "localhost" not in body
    assert json.loads(body)["result"]["capability"] == []


def test_a_derived_endpoint_reaches_every_url_in_the_response():
    """primaryDomain, primaryParUrl and their standby twins all have to point at
    the same reachable place; the firmware uses the domain for getaddrinfo and
    the ParUrl for the upload itself."""
    c = Config(api_url="http://192.0.2.55:8080/6/")
    c.resolve_bucket_endpoint()
    d = Device(device_type="t5", petkit_id=1, serial_number="SN")

    cap = d.to_oss_sts(c.bucket_endpoint, "0123456789abcdef")["result"]["capability"]
    assert cap, "a configured endpoint must yield capabilities"
    for entry in cap:
        # The domain is portless on purpose: the firmware runs it through
        # sscanf + getaddrinfo, which a port breaks.
        assert entry["primaryDomain"] == "https://192.0.2.55/petkit-local/"
        assert entry["standbyDomain"] == "https://192.0.2.55/petkit-local/"
        assert entry["primaryParUrl"] == "https://192.0.2.55:9000/"
        assert entry["standbyParUrl"] == "https://192.0.2.55:9000/"


def test_the_log_upload_token_agrees_with_it():
    """Same endpoint, different firmware path — `logUpload` rebuilds the
    authority from two halves, so an empty endpoint has to yield no token
    rather than a broken one."""
    d = Device(device_type="t5", petkit_id=1, serial_number="SN")
    assert d.to_log_upload_token("")["result"] == {}

    token = d.to_log_upload_token("https://192.0.2.55:9000")["result"]
    assert token, "a real endpoint should produce a token"
    assert "localhost" not in json.dumps(token)
