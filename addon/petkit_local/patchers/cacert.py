"""CA certificate patcher.

Appends the add-on's self-signed TLS certificate to the device's Mozilla CA
bundle (/app/bin/ca.crt) so the cloud binary trusts our local bucket for media
uploads.

All patching is done server-side: download ca.crt → append our cert PEM → upload
back → bind-mount + restart cloud.
"""
from __future__ import annotations

import logging
import os

from petkit_local.patchers.common import md5hex
from petkit_local.patchers.verify import assert_ca_bundle

log = logging.getLogger(__name__)


def patch_ca_bundle(original: bytes | None, our_cert_pem: bytes) -> bytes:
    """Append our self-signed cert PEM to the device's CA bundle.

    Raises:
        ValueError: If either side is not a PEM bundle, or ours is already in.

    Both inputs are validated because the failure mode of not doing so is
    severe: this used to treat an empty `original` as "the device has no CA
    bundle" and return a file containing ONLY our certificate, which — written
    over /app/bin/ca.crt — would leave the device unable to verify any other
    TLS peer. Every device ships a bundle (226 KB on a T5, 11 KB on a D4SH), so
    an empty read means the download failed, not that there is nothing to keep.
    """
    if b"-----BEGIN CERTIFICATE-----" not in our_cert_pem:
        raise ValueError("our_cert_pem is not valid PEM")

    assert_ca_bundle(original or b"", "device ca.crt")
    assert original is not None  # narrowed by assert_ca_bundle

    if our_cert_pem in original:
        raise ValueError("Our certificate is already in the CA bundle")

    original_count = original.count(b"-----BEGIN CERTIFICATE-----")
    patched = original.rstrip() + b"\n\n" + our_cert_pem.strip() + b"\n"
    patched_count = patched.count(b"-----BEGIN CERTIFICATE-----")

    if patched_count != original_count + 1:
        raise RuntimeError(f"Cert count mismatch: {original_count} -> {patched_count} (expected +1)")

    log.info("Patched CA bundle: %d -> %d certs, %d -> %d bytes (md5 %s -> %s)",
             original_count, patched_count, len(original), len(patched),
             md5hex(original), md5hex(patched))
    return patched


def load_our_cert(data_dir: str = "/data") -> bytes:
    """Load the add-on's self-signed cert (generated for the MQTT TLS listener)."""
    cert_path = os.path.join(data_dir, "certs", "broker.crt")
    if not os.path.exists(cert_path):
        raise FileNotFoundError(f"Add-on cert not found at {cert_path} - "
                                "start the add-on once to auto-generate it")
    with open(cert_path, "rb") as f:
        return f.read()


PATCHER_INFO = {
    "id": "cacert",
    "name": "CA Certificate (Bucket TLS)",
    "description": (
        "Required for local storage - the cloud binary verifies the bucket "
        "server's TLS certificate against /app/bin/ca.crt (Mozilla CA bundle). "
        "This patch appends the add-on's self-signed certificate to that bundle "
        "so uploads to our local bucket succeed.\n\n"
        "Apply this together with the Local Storage patch.\n\n"
        "What it does: copies /app/bin/ca.crt to writable storage, appends "
        "the add-on's certificate, then updates the boot wrapper to "
        "bind-mount the patched bundle before the stock init starts cloud."
    ),
    "files": ["ca_patched.crt"],
    # No architecture: this appends PEM text to a certificate bundle and
    # bind-mounts the result. Nothing here is machine code.
    "arch": None,
    # Conservative UI figure: what to tell the user BEFORE we know the model.
    # ca.crt is 226,168 B on a T5 and 11,171 B on a D4SH, plus our PEM.
    "needs_bytes": 524288,
}
