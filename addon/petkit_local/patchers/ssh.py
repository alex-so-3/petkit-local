"""Persistent SSH access patcher.

Installs dropbear on port 22 with public-key authentication, using the boot
hook shared with the other patchers. Dropbear starts before the stock init,
survives reboots and app OTA.

Unlike the binary patchers (mqtt, cloud, cacert), this one needs user input:
a public key. The panel accepts it at apply time, stores it on
`device.config["ssh_pubkey"]`, and writes it to persistent app storage on the
device. Password auth is disabled.

Two pre-built dropbear binaries are shipped — one per CPU family:
  dropbear-mipsel  165 KB  Ingenic MIPS32r2 LE (T5, T6, T7, D4H, D4SH)
  dropbear-armv7   128 KB  ARMv7-A hard-float  (W7H)
Both are static musl builds, UPX-packed, from the same source and patches.
The panel reads the ELF header of the device's own ctrl binary to detect
the architecture and picks the matching dropbear.

If an older `test_case_root` persistence script exists from a prior ssh method,
it is renamed out of the `test_case_*` glob so only one thing listens on port 22.
"""
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from petkit_local.patchers.common import patched_file_path

if TYPE_CHECKING:
    from petkit_local.devices.base import Device

log = logging.getLogger(__name__)

DBKEY_RESERVE_BYTES = 4096
DROPBEAR_NAME = "dropbear"
DBKEY_NAME = "dbkey_ecdsa"
AUTHKEYS_NAME = "authorized_keys"
TEST_CASE_ROOT_NAME = "test_case_root"
TEST_CASE_ROOT_OLD_NAME = "old_test_case_root"

_BIN_DIR = os.path.join(os.path.dirname(__file__), "..", "web", "static", "bin")

ARCH_TO_BINARY: dict[str, str] = {
    "mips": "dropbear-mipsel",
    "arm":  "dropbear-armv7",
}

DROPBEAR_BIN_NAME = "dropbear-mipsel"
DROPBEAR_LOCAL = os.path.join(_BIN_DIR, DROPBEAR_BIN_NAME)


def dropbear_path_for(arch: str) -> str:
    """Return the local filesystem path of the dropbear binary for `arch`."""
    name = ARCH_TO_BINARY.get(arch)
    if not name:
        raise ValueError(f"no dropbear binary for arch {arch!r}")
    return os.path.join(_BIN_DIR, name)


PATCHER_INFO = {
    "id": "ssh",
    "name": "Persistent SSH Access",
    "description": (
        "Installs Dropbear SSH on port 22 with public key authentication. "
        "Uses the device boot wrapper for persistence — SSH starts "
        "before any PetKit processes and survives reboots, app OTA updates, and "
        "factory resets (as long as writable app storage is not formatted).\n\n"
        "A static build is shipped for each CPU family (MIPS and ARM), so this "
        "works on every Linux-based PetKit model. ECDSA host key, RSA/ECDSA "
        "pubkey auth, password auth disabled. The host key is generated once "
        "at install and kept in writable app storage, so the fingerprint stays the same "
        "across reboots.\n\n"
        "Requires a public key — paste one in the field below before applying. "
        "The key is stored per device and reused on re-apply.\n\n"
        "If a test_case_root script from a prior ssh method is found, it is "
        "renamed to old_test_case_root (outside the test_case_* glob) to avoid "
        "running two SSH servers on port 22."
    ),
    "files": [DROPBEAR_NAME, DBKEY_NAME, AUTHKEYS_NAME],
    "arch": None,
    "needs_bytes": 262144,
    "needs_pubkey": True,
}


AUTHKEYS_STAGED_NAME = "authorized_keys"


def build_install_commands(download_base: str, bin_name: str = DROPBEAR_BIN_NAME,
                           device: "Device | None" = None) -> list[str]:
    """Shell commands to install dropbear on the device.

    Each is queued as a separate run_cmd and waited on individually, because
    the heartbeat queue is at-most-once and a chained `&&` that fails partway
    through cannot be retried from the failure point.

    The pubkey is NOT written via `echo '...' > file` — an RSA key is ~580
    bytes, and the heartbeat's run_cmd content field is embedded inside a JSON
    string inside an HTTP response body. Escaping, quoting and shell expansion
    across that many layers turned out to produce garbage on the device (the
    `authorized_keys?Q` incident). Instead the key is staged as a file and
    fetched with wget, exactly like the dropbear binary — the one path that is
    proven to deliver bytes unchanged.

    The host key IS pre-generated and stored on writable app storage so it
    survives every reboot. Without this, `-R` writes to `/tmp` (its default),
    `/tmp` is wiped on reboot, and the key changes every boot — which means
    every reboot prints `REMOTE HOST IDENTIFICATION HAS CHANGED` on the client.
    The generation uses the multi-call trick: `cp dropbear /tmp/dropbearkey`
    then invoke it, because this busybox has `cp` but not `ln`.
    """
    dropbear = patched_file_path(DROPBEAR_NAME, device)
    authkeys = patched_file_path(AUTHKEYS_NAME, device)
    dbkey = patched_file_path(DBKEY_NAME, device)
    test_case_root = patched_file_path(TEST_CASE_ROOT_NAME, device)
    test_case_root_old = patched_file_path(TEST_CASE_ROOT_OLD_NAME, device)

    return [
        # 1. Rename an existing test_case_root out of the glob.
        f'[ -f {test_case_root} ] && mv {test_case_root} {test_case_root_old} || true',

        # 2. Download the dropbear binary.
        f'wget -q -O {dropbear} "{download_base}/{bin_name}" '
        f'&& chmod +x {dropbear}',

        # 3. Download authorized_keys (staged from the stored pubkey).
        f'wget -q -O {authkeys} "{download_base}/{AUTHKEYS_STAGED_NAME}"',

        # 4. Generate a persistent host key if one does not already exist.
        f'[ -f {dbkey} ] || '
        f'(cp {dropbear} /tmp/dropbearkey '
        f'&& /tmp/dropbearkey -t ecdsa -f {dbkey} '
        f'&& rm -f /tmp/dropbearkey)',

        # 5. Start dropbear now (immediate access, before the next reboot).
        f'mkdir -p /tmp/.ssh '
        f'&& cp {authkeys} /tmp/.ssh/authorized_keys '
        f'&& {dropbear} -r {dbkey} -p 22 &',
    ]
