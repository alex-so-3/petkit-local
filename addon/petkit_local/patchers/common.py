"""Shared helpers for device patchers.

- run_cmd delivery over MQTT or the heartbeat queue, whichever the device is
  actually using (same PATH 2 format from note 18)
- Temporary httpd on the device for file download
- File staging on the add-on for device wget
- Unified app_init.sh wrapper generation
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import time
from collections.abc import Sequence
from typing import Any

import aiohttp

from petkit_local.devices.base import Device
log = logging.getLogger(__name__)

DEVICE_HTTPD_PORT = 8888
#: A SECOND httpd, rooted at /tmp, used only to read command output back.
#: It cannot share 8888: that instance is rooted at /app/bin, which is
#: read-only squashfs, so nothing can be written there for it to serve.
DEVICE_PROBE_PORT = 8889
STAGE_DIR = "/tmp/petkit_patcher"

#: app_init.sh is ~1 KB today and every patcher rewrites it.
WRAPPER_RESERVE_BYTES = 4096
#: Slack for a jffs2 erase block and its metadata. Kept small deliberately:
#: jffs2 compresses on write, so `df` UNDER-reports what will actually fit and
#: a generous margin would refuse patches that would have succeeded.
SPACE_MARGIN_BYTES = 65536


class InsufficientDeviceSpace(RuntimeError):
    """Not enough free space on the device for a patch that was about to write."""

# Patched files live in the writable boot override area. Ingenic boards use
# /system; Axera ARM boards use /opt. Codename cannot decide this because D4SH
# ships with both board families under the same type string, so patcher runs
# probe the device filesystem before choosing.
APP_INIT_WRAPPER = "/system/app_init.sh"
OPT_APP_INIT_WRAPPER = "/opt/app_init.sh"
PATCH_STORAGE_CONFIG_KEY = "patch_storage_dir"


def set_patch_storage_dir(device: Device, storage_dir: str) -> None:
    """Remember the probed writable patch storage directory for this device."""
    if storage_dir not in ("/system", "/opt"):
        raise ValueError(f"unsupported patch storage directory: {storage_dir}")
    device.config[PATCH_STORAGE_CONFIG_KEY] = storage_dir


async def detect_patch_storage_dir(device: Device, device_ip: str, *,
                                   bridge: Any = None,
                                   timeout: float = 60.0) -> str:
    """Detect whether patches should persist under /system or /opt.

    The persistent app storage carries user.conf: Ingenic exposes it under
    /system/user.conf, while Axera exposes it under /opt/user.conf. Do not fall
    back to codename guesses here: D4SH may be either MIPS/Ingenic or ARM/Axera.
    """
    command = (
        "[ -f /system/user.conf ] && echo STORAGE /system; "
        "[ -f /opt/user.conf ] && echo STORAGE /opt"
    )
    text = await run_cmd_capture(device, device_ip, command, bridge=bridge,
                                 timeout=timeout)
    for storage_dir in ("/system", "/opt"):
        if f"STORAGE {storage_dir}" in (text or ""):
            set_patch_storage_dir(device, storage_dir)
            return storage_dir

    raise RuntimeError(
        f"could not determine writable patch storage for device {device.petkit_id}: "
        "neither /system/user.conf nor /opt/user.conf exists")


def uses_opt_boot(device: Device | None) -> bool:
    """Whether this device uses the Axera-style /opt app storage."""
    if device is not None:
        storage_dir = device.config.get(PATCH_STORAGE_CONFIG_KEY)
        if storage_dir in ("/system", "/opt"):
            return storage_dir == "/opt"
    return False


def app_init_wrapper_path(device: Device | None = None) -> str:
    return OPT_APP_INIT_WRAPPER if uses_opt_boot(device) else APP_INIT_WRAPPER


def patch_storage_dir(device: Device | None = None) -> str:
    return "/opt" if uses_opt_boot(device) else "/system"


def patched_file_path(filename: str, device: Device | None = None) -> str:
    return f"{patch_storage_dir(device)}/{filename}"


def patcher_device_files(pinfo: dict[str, Any],
                         device: Device | None = None) -> list[str]:
    """Absolute on-device paths for a patcher's declared files.

    Binary/cert patchers declare bare names because their storage directory is
    model-dependent: /system on Ingenic, /opt on Axera. Absolute paths are passed
    through as a defensive guard for future patchers with fixed locations.
    """
    result: list[str] = []
    for name in pinfo["files"]:
        result.append(name if name.startswith("/") else patched_file_path(name, device))
    return result


def build_run_cmd(command: str) -> str:
    """The `msgType` envelope carrying one shell command.

    Uses PATH 2 of the firmware's run_cmd handling - a direct `system()` call
    with no uptime guard and no length limit - which is why the proxy blocks
    this field coming from upstream (see http/proxy.py).
    """
    return json.dumps({
        "msgType": 0,
        "user_cmd": {"run_cmd": command},
    })


async def send_run_cmd(device: Device, command: str, bridge: Any = None) -> str:
    """Deliver a shell command over whichever transport the device is using.

    A device that has joined MQTT STOPS polling the HTTP heartbeat (confirmed
    on a T5: quiet ~40s after CONNECT, for as long as the session lives), so
    the heartbeat queue this used to be the only writer of is never drained and
    every patcher step times out. That is what made patchers stop working once
    the mqtt patcher was applied.

    MQTT delivery goes to `/{pk}/{dn}/user/get`, the one downstream topic that
    carries the same `msgType` envelope the heartbeat does.

    **Confirmed on hardware (2026-07-29): a T5 executes a `user_cmd.run_cmd`
    delivered this way.** It was shipped as an assumption and has since been
    tested, so treat this path as working rather than provisional. Worth
    recording that the real cloud has never been seen doing it — 0 run_cmd
    frames in 1152 captured proxy records — so this is our use of the transport,
    not a reproduction of PetKit's.

    Returns:
        The transport used, "mqtt" or "heartbeat" — logged by the caller so a
        failed run can be read afterwards without guessing which path it took.
    """
    content = build_run_cmd(command)

    on_mqtt = bool(getattr(device, "mqtt_connected", False)) and bridge is not None
    if on_mqtt:
        try:
            if await bridge.publish_user_get(device, json.loads(content)):
                log.info("Patcher: run_cmd over MQTT for device %d: %.120s",
                         device.petkit_id, command)
                return "mqtt"
        except Exception:
            # Never fatal: falling through to the queue is strictly better than
            # aborting a multi-step patcher run halfway.
            log.exception("Patcher: MQTT run_cmd failed for device %d, queueing instead",
                          device.petkit_id)

    device.command_queue.append(content)
    log.info("Patcher: queued run_cmd for device %d: %.120s", device.petkit_id, command)
    return "heartbeat"


async def wait_for_heartbeat(device: Device, timeout: float = 30) -> bool:
    """Wait until the device picks up a QUEUED command via heartbeat.

    Returns True immediately when the queue is empty, which is also the MQTT
    case: `send_run_cmd` published rather than queued, and that transport has
    no delivery report to wait for — the broker accepts a publish whether or
    not anyone is subscribed. So a True here means "nothing is still waiting to
    be collected", never "the device ran it"; the patcher steps prove that for
    themselves by downloading the result back off the device.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not device.command_queue:
            return True
        await asyncio.sleep(2)
    return not device.command_queue


async def download_from_device(device_ip: str, filename: str, timeout: float = 30,
                               port: int = DEVICE_HTTPD_PORT) -> bytes:
    """Download a file from a temporary busybox httpd on the device.

    Args:
        port: Which httpd to ask. The default is the file server rooted at
            /app/bin; `run_cmd_capture` uses `DEVICE_PROBE_PORT`, rooted at
            /tmp, because command output cannot be written under /app/bin.
    """
    url = f"http://{device_ip}:{port}/{filename}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status != 200:
                raise RuntimeError(f"GET {url} returned {resp.status}")
            data = await resp.read()
            log.info("Patcher: downloaded %s from device (%d bytes)", filename, len(data))
            return data


async def run_cmd_capture(device: Device, device_ip: str, command: str, *,
                          bridge: Any = None, port: int = DEVICE_PROBE_PORT,
                          timeout: float = 60.0, poll_interval: float = 3.0) -> str | None:
    """Run a shell command on the device and return what it printed.

    `send_run_cmd` is fire-and-forget and `wait_for_heartbeat` only reports that
    the queue drained, so until now nothing here could read a command's OUTPUT.
    This closes that gap the only way the device allows: the command redirects
    into /tmp (tmpfs — /app/bin is read-only squashfs), a second busybox httpd
    serves /tmp, and we poll for the file.

    Three properties make the result trustworthy despite the transport having no
    delivery acknowledgement:

    * **A stale file cannot be misread.** The filename carries 16 random hex
      characters minted in THIS call, and the same token is repeated in a
      sentinel line inside the file. A leftover from an earlier run has neither.
    * **A partial file cannot be misread.** Output is written to `.part` and
      renamed — atomic within one tmpfs — before the httpd is started, and the
      reader additionally requires the trailing sentinel.
    * **stderr is kept** (`2>&1`), so `df: /system: No such file` ends up in the
      returned text and therefore in the operator's log, instead of vanishing.

    Returns:
        The captured output with the sentinel stripped, or None if the device
        never produced it within `timeout`. Never raises for a device-side
        failure — the caller decides what an unknown result means.
    """
    nonce = secrets.token_hex(8)
    name = f"pk_{nonce}"
    sentinel = f"__PK_END_{nonce}"
    out = f"/tmp/{name}"

    # One command: run, mark the end, publish atomically, then serve. Starting
    # the httpd last means a 200 can only ever be a complete file. Re-issuing
    # `busybox httpd` when one is already bound simply fails and leaves the
    # running one serving, which is exactly what we want.
    await send_run_cmd(device, (
        f"{{ {command}; }} > {out}.part 2>&1; "
        f"echo {sentinel} >> {out}.part; "
        f"mv {out}.part {out}; "
        f"busybox httpd -p {port} -h /tmp"
    ), bridge)

    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            await asyncio.sleep(poll_interval)
            try:
                raw = await download_from_device(device_ip, name, timeout=10, port=port)
            except Exception:
                continue  # not written yet, or the httpd has not come up
            text = raw.decode("utf-8", "replace")
            if text.rstrip().endswith(sentinel):
                log.info("Patcher: captured %d bytes of output for device %d",
                         len(raw), device.petkit_id)
                return text.rstrip()[: -len(sentinel)].rstrip("\n")
        log.warning("Patcher: no output from device %d within %.0fs for: %.80s",
                    device.petkit_id, timeout, command)
        return None
    finally:
        # Best-effort: a leaked file is 1 KB of tmpfs and a leaked httpd is
        # harmless, but neither should outlive the run if the device is healthy.
        await send_run_cmd(device, f"rm -f {out} {out}.part", bridge)


def parse_df_available_bytes(text: str, mount: str = "/system") -> int | None:
    """Available bytes on the filesystem holding `mount`, from `df -k` output.

    Parsed here rather than on the device because busybox on these boxes is
    stripped — no `cut`, `head`, `tr` or `grep -E` — so an `awk`/pipeline
    one-liner would be a guess about which applets survived. A bare `df` always
    works.

    Fields are read from the RIGHT (`Available` is third from the end), because
    busybox wraps a long filesystem name onto its own line, which shifts every
    column when counting from the left.

    Returns:
        Available bytes, or None if `mount` is absent or nothing parses — which
        callers must treat as "unknown", not as "full".
    """
    if not text:
        return None
    best: tuple[int, int] | None = None  # (len(mountpoint), available_bytes)
    pending: list[str] = []
    for line in text.splitlines():
        fields = pending + line.split()
        pending = []
        if not fields or fields[0] in ("Filesystem", "df:"):
            continue
        # A wrapped filesystem name is a line of its own; hold it for the next.
        if len(fields) < 6:
            pending = fields
            continue
        mounted_on = fields[-1]
        if not (mount == mounted_on or mount.startswith(mounted_on.rstrip("/") + "/")):
            continue
        try:
            available_kb = int(fields[-3])
        except ValueError:
            continue
        # Longest matching mountpoint wins: /system beats / for /system/foo.
        if best is None or len(mounted_on) > best[0]:
            best = (len(mounted_on), available_kb * 1024)
    return best[1] if best else None


def parse_wc_c_sizes(text: str) -> dict[str, int]:
    """Map path -> size from `wc -c` output.

    `wc -c` is used rather than `ls -l` because its output is two fields in a
    fixed order regardless of locale, permissions or timestamp format. A file
    that does not exist produces an error line and is simply absent from the
    result, which is the correct reading: nothing there to reclaim.
    """
    sizes: dict[str, int] = {}
    for line in (text or "").splitlines():
        fields = line.split()
        if len(fields) != 2 or fields[1] == "total":
            continue
        try:
            sizes[fields[1]] = int(fields[0])
        except ValueError:
            continue
    return sizes


def required_free_bytes(write_bytes: int, existing_bytes: int = 0) -> int:
    """Free space a patch needs, crediting the file it will overwrite.

    The credit is not an optimisation. Re-applying the mqtt patcher overwrites
    an existing 1.4 MB /system/ctrl_patched, and that space comes back; without
    the credit the check would fail on precisely the devices where the patch is
    already installed and working.
    """
    return max(0, write_bytes - existing_bytes) + WRAPPER_RESERVE_BYTES + SPACE_MARGIN_BYTES


def _fmt_bytes(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


async def ensure_space_for(device: Device, device_ip: str, *, write_bytes: int,
                           targets: Sequence[str] = (), bridge: Any = None,
                           mount: str = "/system", timeout: float = 60.0) -> str:
    """Check the device has room for a patch, before anything is written.

    Reads `df` and the current sizes of `targets` in ONE round trip, so a
    re-apply is credited the space its own previous output already occupies.

    Returns:
        A human-readable line for the operator's log — including the case where
        the probe did not answer.

    Raises:
        InsufficientDeviceSpace: Only when the free space is KNOWN and is too
            small. A probe that fails to answer yields a warning and lets the
            patch continue: "unknown" is not evidence of danger, and refusing to
            patch a device whose busybox lacks `df` would break a working
            feature to enforce a check that never ran.
    """
    quoted = " ".join(targets)
    command = f"df -k {mount}" + (f"; wc -c {quoted}" if quoted else "")
    text = await run_cmd_capture(device, device_ip, command, bridge=bridge, timeout=timeout)

    if text is None:
        return (f"WARNING: could not read {mount} free space (no answer from the device) "
                "— proceeding without the space check")

    free = parse_df_available_bytes(text, mount)
    if free is None:
        return (f"WARNING: could not parse {mount} free space from the device's df output "
                "— proceeding without the space check")

    existing = sum(parse_wc_c_sizes(text).get(t, 0) for t in targets)
    needed = required_free_bytes(write_bytes, existing)
    if free < needed:
        raise InsufficientDeviceSpace(
            f"{mount} has {_fmt_bytes(free)} free but this patch needs "
            f"{_fmt_bytes(needed)} ({_fmt_bytes(write_bytes)} to write"
            + (f", {_fmt_bytes(existing)} reclaimed from the current version" if existing else "")
            + f", plus {_fmt_bytes(WRAPPER_RESERVE_BYTES + SPACE_MARGIN_BYTES)} for the boot "
            "wrapper and filesystem overhead)"
        )
    return f"{mount}: {_fmt_bytes(free)} free, needs {_fmt_bytes(needed)}"


def stage_file(filename: str, data: bytes) -> str:
    """Write a patched file to the staging directory for the device to wget."""
    os.makedirs(STAGE_DIR, exist_ok=True)
    path = os.path.join(STAGE_DIR, filename)
    with open(path, "wb") as f:
        f.write(data)
    log.info("Patcher: staged %s (%d bytes, md5=%s)", filename, len(data),
             hashlib.md5(data).hexdigest())
    return path


def cleanup_staged(filename: str) -> None:
    """Drop a staged file once the device has fetched it.

    A file that is already gone is not an error: the patcher's cleanup runs on
    both the success and failure paths.
    """
    path = os.path.join(STAGE_DIR, filename)
    try:
        os.unlink(path)
    except OSError:
        pass


def get_staged_path(filename: str) -> str | None:
    """Path of a staged file, or None if it is not staged right now.

    Note that `/patcher/download` does NOT go through this: it resolves the
    name against `STAGE_DIR` with `safe_join` (see http/server.py), so this
    helper exists for callers that need to ask without serving.
    """
    path = os.path.join(STAGE_DIR, filename)
    return path if os.path.isfile(path) else None


def md5hex(data: bytes) -> str:
    """MD5 of `data` as lowercase hex - a transfer check, never a security one."""
    return hashlib.md5(data).hexdigest()


# --------------- Unified /system/app_init.sh wrapper -------------------------
# Boot chain: kernel → system_init.sh → if /system/app_init.sh exists, run it
# INSTEAD of /app/script/app_init.sh (stock, read-only squashfs).
#
# The wrapper does bind-mounts for all active patches BEFORE sourcing the stock
# init, so all patched binaries are in place when app_start.sh starts processes.
# This avoids the test_case_* timing problem (test_case hooks run AFTER
# processes are already loaded into memory).

WRAPPER_HEADER_TEMPLATE = """\
#!/bin/sh
# {wrapper} - petkit-local patcher wrapper
# Auto-generated. Bind-mounts patched files before running the stock init.
"""

WRAPPER_FOOTER = """\

# Run the stock init (which starts all processes using the patched files)
. /app/script/app_init.sh
"""

# Per-patcher bind-mount blocks. Each block is formatted with `store`; write
# literal shell braces as `{{` and `}}`.
# Each is a tuple (patcher_id, shell_lines).
# Only active patchers are included in the generated wrapper.
#
# /app/bin/watchdog supervises media, cloud, agora and logUpload. Its liveness
# test is `lsof |grep /app/bin/<name> |wc -l`, and busybox lsof prints
# `PID <TAB> /proc/PID/exe <TAB> <fd target>` — so it matches on the process's
# EXECUTABLE PATH, not on an open descriptor. Two consequences:
#
#   * bind-mounting a replacement ELF is invisible to it: exec'ing the
#     mountpoint gives /proc/PID/exe == the mountpoint path. Confirmed live —
#     /system/cloud_patched running as /app/bin/cloud reports /app/bin/cloud.
#   * a shell script can never satisfy it. The kernel execs the interpreter,
#     so /proc/PID/exe is /bin/busybox whatever the script is called. A dummy
#     `while true; do sleep 3600; done` over /app/bin/agora reads as dead on
#     every check, and the watchdog respawns it forever.
#
# Hence camera bind-mounts the real tserver binary over agora rather than a
# placeholder: exec'ing ./agora starts the local streamer, the lsof check
# passes, and the watchdog now supervises tserver for us.
BIND_MOUNT_TEMPLATES = {
    "mqtt": (
        "# MQTT TLS bypass: patched ctrl accepts any broker certificate\n"
        "mount --bind {store}/ctrl_patched /app/bin/ctrl\n"
    ),
    "cloud": (
        "# Local storage: patched cloud accepts LAN IPs + skips TLS verify\n"
        "mount --bind {store}/cloud_patched /app/bin/cloud\n"
    ),
    "cacert": (
        "# CA cert: cloud trusts our self-signed bucket certificate\n"
        "mount --bind {store}/ca_patched.crt /app/bin/ca.crt\n"
    ),
    "camera": (
        "# Local camera: stock app_start.sh runs ./agora, which is tserver now\n"
        "mount --bind /app/bin/tserver /app/bin/agora\n"
    ),
    # ssh: no bind-mount needed (dropbear is not replacing a stock binary)
}

# Pre-init commands (run BEFORE stock app_init.sh is sourced).
# SSH needs this — dropbear should be up even if stock init fails.
PRE_INIT_BLOCKS = {
    "ssh": (
        "# Persistent SSH (dropbear on port 22)\n"
        "mkdir -p /tmp/.ssh\n"
        "cp {store}/authorized_keys /tmp/.ssh/authorized_keys\n"
        "{store}/dropbear -r {store}/dbkey_ecdsa -p 22 &\n"
    ),
}

def generate_app_init_wrapper(active_patchers: set[str],
                              device: Device | None = None) -> str:
    """Generate the app_init.sh wrapper for the given set of active
    patchers. Returns the shell script content.

    Everything happens before the stock init is sourced, because the stock
    init is what starts the processes: a bind-mount that lands after it would
    only take effect on the following boot. There is deliberately no post-init
    phase — camera used to start tserver there, and now reaches it through the
    agora bind-mount instead, which also puts it under the watchdog.
    """
    lines = [WRAPPER_HEADER_TEMPLATE.format(
        wrapper=app_init_wrapper_path(device))]
    store = patch_storage_dir(device)
    # Pre-init: things that must run before stock init (SSH, etc.)
    for pid in ("ssh",):
        if pid in active_patchers and pid in PRE_INIT_BLOCKS:
            lines.append(PRE_INIT_BLOCKS[pid].format(store=store))
    for pid in ("mqtt", "cloud", "cacert", "camera"):
        if pid in active_patchers and pid in BIND_MOUNT_TEMPLATES:
            lines.append(BIND_MOUNT_TEMPLATES[pid].format(store=store))
    lines.append(WRAPPER_FOOTER)
    return "".join(lines)


def build_wrapper_upload_cmd(active_patchers: set[str],
                             device: Device | None = None) -> str:
    """Build a shell command that writes the boot wrapper on the device."""
    wrapper = app_init_wrapper_path(device)
    content = generate_app_init_wrapper(active_patchers, device)
    escaped = content.replace("'", "'\\''")
    return f"printf '{escaped}' > {wrapper} && chmod +x {wrapper}"


def build_wrapper_remove_cmd(device: Device | None = None) -> str:
    """Shell command to remove the wrapper (reverts to stock init on next boot)."""
    return f"rm -f {app_init_wrapper_path(device)}"
