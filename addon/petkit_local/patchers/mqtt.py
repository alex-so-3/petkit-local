"""MQTT cert bypass patcher.

Patches one function in the ctrl binary — mbedtls_x509_crt_verify_with_profile
— so the device accepts our self-signed broker certificate.

This function is called DURING the TLS handshake by ssl_parse_certificate to
validate the server's cert chain against the built-in ali_ca_cert (GlobalSign
Root CA for Aliyun IoT). With the stock code our self-signed cert fails here,
the handshake returns a non-zero error, and the Aliyun Link SDK reports
-0x0F23. Zeroing the return value AND the *flags output makes the handshake
succeed; mbedtls_ssl_get_verify_result (post-handshake readback) then returns 0
naturally because ssl_parse_certificate stored the zeroed flags.

Verified: patching verify_with_profile alone is sufficient. The earlier attempt
patching only get_verify_result failed because that function is behind the
handshake gate — it is never reached when the handshake itself fails.

The function is statically linked into ctrl (no shared library).
"""
from __future__ import annotations

import io
import logging

from petkit_local.patchers.common import md5hex
from petkit_local.patchers.verify import (
    MIPS_ELF_BASE, assert_arm_elf, assert_mips_elf, describe_elf, elf_arch,
)

log = logging.getLogger(__name__)

SYMBOL_NAME = "mbedtls_x509_crt_verify_with_profile"

# Replacement (16 bytes, overwrites the function prologue):
#
# MIPS o32 ABI: args 1-4 in $a0-$a3, arg 5+ on stack.
#   arg6 = uint32_t *flags  →  at sp+0x14 on entry
#
#   lw    $v1, 0x14($sp)   → load flags pointer (arg6)
#   sw    $zero, 0($v1)    → *flags = 0
#   jr    $ra              → return
#   move  $v0, $zero       → return value = 0 (delay slot)
PATCH_BYTES = (
    b'\x14\x00\xa3\x8f'   # lw   $v1, 0x14($sp)
    b'\x00\x00\x60\xac'   # sw   $zero, 0($v1)
    b'\x08\x00\xe0\x03'   # jr   $ra
    b'\x21\x10\x00\x00'   # move $v0, $zero
)

#: The third instruction of any MIPS PIC function prologue — `addu $gp,$gp,$t9`.
#: Constant across all builds (the first two words carry per-binary $gp offsets).
_GP_SETUP_INSN = b'\x21\xe0\x99\x03'


def _find_via_dynsym(data: bytes) -> int | None:
    try:
        from elftools.elf.elffile import ELFFile
    except ImportError:
        return None
    try:
        elf = ELFFile(io.BytesIO(data))
        dynsym = elf.get_section_by_name(".dynsym")
        if not dynsym:
            return None
        for sym in dynsym.iter_symbols():
            if sym.name == SYMBOL_NAME and sym["st_info"]["type"] == "STT_FUNC":
                vaddr = sym["st_value"]
                offset = vaddr - MIPS_ELF_BASE
                if offset < 0 or offset >= len(data):
                    log.warning("%s symbol at vaddr 0x%x maps to offset 0x%x, "
                                "outside file (%d bytes)", SYMBOL_NAME, vaddr,
                                offset, len(data))
                    return None
                log.info("Found %s via .dynsym at vaddr 0x%x (file offset 0x%x)",
                         SYMBOL_NAME, vaddr, offset)
                return offset
    except Exception as e:
        log.warning("pyelftools parsing failed: %s", e)
    return None


def _is_mips_pic_prologue(data: bytes, offset: int) -> bool:
    """Whether the 16 bytes at `offset` match the shape of a MIPS PIC prologue.

    Every PIC function starts with:
      lui   $gp, ANY       → ?? ?? 1c 3c
      addiu $gp, $gp, ANY  → ?? ?? 9c 27
      addu  $gp, $gp, $t9  → 21 e0 99 03  (constant)
      addiu $sp, $sp, ANY  → ?? ?? bd 27
    """
    if offset + 16 > len(data):
        return False
    w = data[offset:offset + 16]
    return (w[2:4] == b'\x1c\x3c'       # lui $gp
            and w[6:8] == b'\x9c\x27'   # addiu $gp,$gp
            and w[8:12] == _GP_SETUP_INSN
            and w[14:16] == b'\xbd\x27')  # addiu $sp,$sp


def _find_via_pattern(data: bytes) -> int | None:
    """Search for the function by its unique prologue shape near the symbol name."""
    name_bytes = SYMBOL_NAME.encode()
    name_pos = data.find(name_bytes)
    if name_pos == -1:
        return None

    start = 0
    candidates: list[int] = []
    while True:
        idx = data.find(_GP_SETUP_INSN, start)
        if idx == -1:
            break
        prologue_start = idx - 8
        if prologue_start >= 0 and _is_mips_pic_prologue(data, prologue_start):
            candidates.append(prologue_start)
        start = idx + 4

    if not candidates:
        return None
    if len(candidates) == 1:
        log.info("Found %s via byte pattern at file offset 0x%x", SYMBOL_NAME,
                 candidates[0])
        return candidates[0]

    # Many PIC prologues exist; narrow by proximity to the symbol string.
    nearby = [c for c in candidates if abs(c - name_pos) < 0x4000]
    if len(nearby) == 1:
        log.info("Found %s via byte pattern (proximity) at file offset 0x%x",
                 SYMBOL_NAME, nearby[0])
        return nearby[0]

    log.warning("Found %d prologue candidates for %s, %d near the string — "
                "ambiguous", len(candidates), SYMBOL_NAME, len(nearby))
    return None


# --- ARM Thumb: same function, different encoding --------------------------
# ARM EABI: args 1-4 in r0-r3, args 5-6 on stack at sp+0 and sp+4.
#   arg6 = uint32_t *flags  →  at [sp, #4] on entry
ARM_PATCH_BYTES = (
    b'\x01\x99'   # ldr  r1, [sp, #4]  — flags ptr (arg6)
    b'\x00\x20'   # movs r0, #0        — return 0
    b'\x08\x60'   # str  r0, [r1]      — *flags = 0
    b'\x70\x47'   # bx   lr            — return
)

# The function's Thumb2 prologue on the W7H 456 firmware. Unique in the binary
# (verified: exactly 1 match in 939,176 bytes). The function has no .dynsym
# symbol and its name string does not appear in the binary at all, so this
# prologue is the only search key.
_ARM_PROLOGUE = bytes.fromhex("2de9f04f8bb0dde91484")


def _find_arm_verify(data: bytes) -> int:
    """Find mbedtls_x509_crt_verify_with_profile in an ARM ctrl binary."""
    idx = data.find(ARM_PATCH_BYTES)
    if idx != -1:
        log.info("Found already-patched ARM verify stub at 0x%x", idx)
        return idx

    candidates: list[int] = []
    pos = 0
    while True:
        idx = data.find(_ARM_PROLOGUE, pos)
        if idx == -1:
            break
        candidates.append(idx)
        pos = idx + 2
    if len(candidates) == 1:
        log.info("Found ARM verify prologue at file offset 0x%x", candidates[0])
        return candidates[0]
    if not candidates:
        raise ValueError(
            f"Cannot find {SYMBOL_NAME} in ARM ctrl binary — "
            "prologue pattern not found")
    raise ValueError(
        f"Cannot find {SYMBOL_NAME} in ARM ctrl binary — "
        f"{len(candidates)} prologue matches, ambiguous")


def find_offset(data: bytes) -> int:
    """Find mbedtls_x509_crt_verify_with_profile in the ctrl binary."""
    offset = _find_via_dynsym(data)
    if offset is None:
        offset = _find_via_pattern(data)
    if offset is None:
        raise ValueError(f"Cannot find {SYMBOL_NAME} in ctrl binary — "
                         "neither .dynsym nor byte pattern matched")
    return offset


def patch_ctrl(data: bytes) -> tuple[bytes, int]:
    """Patch the ctrl binary. Returns (patched_data, offset).

    Works on both MIPS (Ingenic) and ARM (Axera) binaries.
    The architecture is detected from the ELF header, and the right patch
    bytes and search strategy are chosen automatically.

    Raises:
        ValueError: If the architecture is unsupported, the function cannot be
            located, or the binary is already patched.
    """
    arch = elf_arch(data)
    if arch == "mips":
        assert_mips_elf(data, "ctrl", exec_only=True)
        offset = find_offset(data)
        patch = PATCH_BYTES
    elif arch == "arm":
        assert_arm_elf(data, "ctrl", exec_only=True)
        offset = _find_arm_verify(data)
        patch = ARM_PATCH_BYTES
    else:
        raise ValueError(f"ctrl is not a supported architecture — "
                         f"got {describe_elf(data)}")

    if data[offset:offset + len(patch)] == patch:
        raise ValueError("ctrl binary is already patched")

    patched = bytearray(data)
    patched[offset:offset + len(patch)] = patch
    patched = bytes(patched)

    if len(patched) != len(data):
        raise RuntimeError(f"Size changed: {len(data)} -> {len(patched)}")

    log.info("Patched %s at offset 0x%x (%s, md5 %s -> %s)",
             SYMBOL_NAME, offset, arch, md5hex(data), md5hex(patched))
    return patched, offset


PATCHER_INFO = {
    "id": "mqtt",
    "name": "MQTT TLS Bypass",
    "description": (
        "Patches the ctrl binary so it accepts any MQTT broker certificate "
        "(mbedtls_x509_crt_verify_with_profile → return 0). This is the "
        "function that validates the server's cert chain during the TLS "
        "handshake; with the stock code, our self-signed cert is rejected "
        "before any MQTT byte is exchanged.\n\n"
        "This allows the device to connect to petkit-local's embedded MQTT "
        "broker over TLS, enabling real-time commands and event streaming "
        "instead of the slower HTTP heartbeat polling.\n\n"
        "Works on both MIPS (Ingenic) and ARM (Axera) devices — the right "
        "patch is chosen from the binary itself.\n\n"
        "What it does: copies /app/bin/ctrl to writable storage, patches "
        "the x509 certificate chain verification function (16 bytes on MIPS, "
        "8 bytes on ARM), then updates the boot wrapper to bind-mount the "
        "patched binary before the stock init starts ctrl."
    ),
    "files": ["ctrl_patched"],
    "arch": None,
    # Conservative UI figure: what to tell the user BEFORE we know the model.
    # ctrl is 1,420,656 B on a T5 and 903,148 B on a D4SH; the patch
    # preserves size, so the write is one full copy of the binary.
    "needs_bytes": 2097152,
    "check_field": "mqtt_connected",
}
