"""Tests for petkit_local/patchers/common.py — run_cmd queueing, file staging,
and the unified app_init.sh wrapper generator."""
import json
import os

from petkit_local.devices.base import Device
from petkit_local.patchers.common import (
    build_run_cmd,
    send_run_cmd,
    stage_file,
    cleanup_staged,
    get_staged_path,
    md5hex,
    generate_app_init_wrapper,
    build_wrapper_upload_cmd,
    build_wrapper_remove_cmd,
    detect_patch_storage_dir,
    APP_INIT_WRAPPER,
    OPT_APP_INIT_WRAPPER,
    app_init_wrapper_path,
    patched_file_path,
    patcher_device_files,
    set_patch_storage_dir,
)
from petkit_local.patchers.ssh import build_install_commands


# --- run_cmd delivery ---

async def test_run_cmd_format():
    d = Device(device_type="t5", petkit_id=1)
    assert await send_run_cmd(d, "echo hello") == "heartbeat"
    assert len(d.command_queue) == 1
    payload = json.loads(d.command_queue[0])
    assert payload["msgType"] == 0
    assert payload["user_cmd"]["run_cmd"] == "echo hello"


async def test_run_cmd_path2_no_type():
    d = Device(device_type="t5", petkit_id=1)
    await send_run_cmd(d, "reboot")
    payload = json.loads(d.command_queue[0])
    assert "type" not in payload
    assert "timestamp" not in payload


async def test_queue_multiple_commands():
    d = Device(device_type="t5", petkit_id=1)
    await send_run_cmd(d, "cmd1")
    await send_run_cmd(d, "cmd2")
    assert len(d.command_queue) == 2
    assert json.loads(d.command_queue[0])["user_cmd"]["run_cmd"] == "cmd1"
    assert json.loads(d.command_queue[1])["user_cmd"]["run_cmd"] == "cmd2"


class _FakeBridge:
    """Records what would have gone to /{pk}/{dn}/user/get."""

    def __init__(self, ok=True):
        self.sent = []
        self.ok = ok

    async def publish_user_get(self, device, payload):
        self.sent.append((device.petkit_id, payload))
        return self.ok


async def test_a_device_on_mqtt_is_sent_the_command_not_queued():
    """A device that has joined MQTT stops polling the heartbeat, so anything
    left in `command_queue` is never collected — the whole reason patchers
    stopped working once the mqtt patcher was applied. The payload is identical
    to the heartbeat's; only the transport differs."""
    d = Device(device_type="t5", petkit_id=1)
    d.mqtt_connected = True
    bridge = _FakeBridge()

    assert await send_run_cmd(d, "reboot", bridge) == "mqtt"
    assert d.command_queue == []
    _did, payload = bridge.sent[0]
    assert payload == json.loads(build_run_cmd("reboot"))


async def test_mqtt_delivery_falls_back_to_the_queue_when_it_cannot_publish():
    """`mqtt_connected` can be stale, and a publish with no client must not
    abort a multi-step patcher run halfway."""
    d = Device(device_type="t5", petkit_id=1)
    d.mqtt_connected = True

    assert await send_run_cmd(d, "reboot", _FakeBridge(ok=False)) == "heartbeat"
    assert len(d.command_queue) == 1

    class _Boom:
        async def publish_user_get(self, device, payload):
            raise RuntimeError("no client")

    d2 = Device(device_type="t5", petkit_id=2)
    d2.mqtt_connected = True
    assert await send_run_cmd(d2, "reboot", _Boom()) == "heartbeat"
    assert len(d2.command_queue) == 1


async def test_no_bridge_still_uses_the_heartbeat_queue():
    d = Device(device_type="t5", petkit_id=1)
    d.mqtt_connected = True
    assert await send_run_cmd(d, "reboot", None) == "heartbeat"
    assert len(d.command_queue) == 1


# --- file staging ---

def test_stage_and_get():
    data = b"test content"
    path = stage_file("test_stage.bin", data)
    assert os.path.isfile(path)
    assert get_staged_path("test_stage.bin") == path
    with open(path, "rb") as f:
        assert f.read() == data
    cleanup_staged("test_stage.bin")
    assert get_staged_path("test_stage.bin") is None


def test_cleanup_nonexistent_is_safe():
    cleanup_staged("does_not_exist_12345.bin")


def test_get_staged_returns_none_for_missing():
    assert get_staged_path("no_such_file_xyz.bin") is None


# --- md5hex ---

def test_md5hex():
    assert md5hex(b"") == "d41d8cd98f00b204e9800998ecf8427e"
    assert md5hex(b"hello") == "5d41402abc4b2a76b9719d911017c592"


# --- wrapper generation ---

def test_wrapper_empty_set():
    w = generate_app_init_wrapper(set())
    assert "#!/bin/sh" in w
    assert ". /app/script/app_init.sh" in w
    assert "mount --bind" not in w
    assert "tserver" not in w


def test_wrapper_mqtt_only():
    w = generate_app_init_wrapper({"mqtt"})
    assert "mount --bind /system/ctrl_patched /app/bin/ctrl" in w
    assert ". /app/script/app_init.sh" in w
    assert "ca_patched" not in w
    assert "tserver" not in w


def test_wrapper_cacert_only():
    w = generate_app_init_wrapper({"cacert"})
    assert "mount --bind /system/ca_patched.crt /app/bin/ca.crt" in w
    assert "ctrl_patched" not in w
    assert "tserver" not in w


def test_wrapper_camera_only():
    w = generate_app_init_wrapper({"camera"})
    assert "mount --bind /app/bin/tserver /app/bin/agora" in w
    lines = w.split("\n")
    bind_idx = next(i for i, l in enumerate(lines) if "mount --bind" in l and "agora" in l)
    stock_idx = next(i for i, l in enumerate(lines) if l.strip().startswith(". /app/script/app_init.sh"))
    # The bind-mount must precede the stock init: app_start.sh's `./agora &` is
    # what actually launches tserver, so a later mount would be a boot too late.
    assert bind_idx < stock_idx


def test_wrapper_camera_never_uses_a_shell_placeholder():
    """The watchdog identifies processes by /proc/PID/exe, which for any script
    is the interpreter — so a shell stand-in over /app/bin/agora always reads as
    dead and gets respawned forever. Only a real ELF may be mounted there."""
    w = generate_app_init_wrapper({"camera"})
    agora_mount = next(l for l in w.split("\n") if "mount --bind" in l and "agora" in l)
    assert not agora_mount.rstrip().endswith(".sh")
    assert "fake_daemon" not in w


def test_wrapper_all_three():
    w = generate_app_init_wrapper({"mqtt", "cacert", "camera"})
    assert "mount --bind /system/ctrl_patched /app/bin/ctrl" in w
    assert "mount --bind /system/ca_patched.crt /app/bin/ca.crt" in w
    assert "mount --bind /app/bin/tserver /app/bin/agora" in w
    assert ". /app/script/app_init.sh" in w


def test_wrapper_bind_mounts_before_stock_init():
    for combo in [{"mqtt"}, {"cacert"}, {"camera"}, {"mqtt", "cacert", "camera"}]:
        w = generate_app_init_wrapper(combo)
        lines = w.split("\n")
        stock_line = next(i for i, l in enumerate(lines) if ". /app/script/app_init.sh" in l)
        for i, l in enumerate(lines):
            if "mount --bind" in l:
                assert i < stock_line, f"bind-mount at line {i} is AFTER stock init at line {stock_line} for {combo}"


def test_wrapper_order_is_deterministic():
    w1 = generate_app_init_wrapper({"camera", "mqtt", "cacert"})
    w2 = generate_app_init_wrapper({"cacert", "camera", "mqtt"})
    assert w1 == w2
    lines = [l for l in w1.split("\n") if "mount --bind" in l]
    assert "ctrl" in lines[0]
    assert "ca.crt" in lines[1]
    assert "agora" in lines[2]


def test_wrapper_camera_tserver_only_when_camera_active():
    assert "tserver" not in generate_app_init_wrapper({"mqtt"})
    assert "tserver" not in generate_app_init_wrapper({"mqtt", "cacert"})
    assert "tserver" in generate_app_init_wrapper({"camera"})
    assert "tserver" in generate_app_init_wrapper({"mqtt", "camera"})


def test_wrapper_has_no_post_init_phase():
    """Everything the wrapper does must land before the stock init sources
    app_start.sh, which is what starts the processes."""
    for combo in [set(), {"mqtt"}, {"camera"}, {"ssh", "cloud", "cacert", "camera"}]:
        w = generate_app_init_wrapper(combo)
        tail = w.split(". /app/script/app_init.sh", 1)[1]
        assert tail.strip() == "", f"content after the stock init for {combo}: {tail!r}"


def test_wrapper_unknown_patcher_ignored():
    w = generate_app_init_wrapper({"mqtt", "bogus_patcher"})
    assert "mount --bind /system/ctrl_patched /app/bin/ctrl" in w
    assert "bogus" not in w


def test_axera_wrapper_uses_opt_storage():
    d = Device(device_type="d4sh", petkit_id=7)
    set_patch_storage_dir(d, "/opt")
    w = generate_app_init_wrapper({"cloud", "cacert"}, d)
    assert "mount --bind /opt/cloud_patched /app/bin/cloud" in w
    assert "mount --bind /opt/ca_patched.crt /app/bin/ca.crt" in w
    assert "/system/cloud_patched" not in w
    assert ". /app/script/app_init.sh" in w


def test_axera_paths_use_opt_boot_override():
    axera = Device(device_type="d4sh", petkit_id=7)
    set_patch_storage_dir(axera, "/opt")
    ing = Device(device_type="d4sh", petkit_id=8)
    set_patch_storage_dir(ing, "/system")
    assert app_init_wrapper_path(axera) == OPT_APP_INIT_WRAPPER
    assert patched_file_path("cloud_patched", axera) == "/opt/cloud_patched"
    assert app_init_wrapper_path(ing) == APP_INIT_WRAPPER
    assert patched_file_path("cloud_patched", ing) == "/system/cloud_patched"
    assert app_init_wrapper_path() == APP_INIT_WRAPPER
    assert patched_file_path("cloud_patched") == "/system/cloud_patched"


async def test_user_conf_probe_prefers_system_when_present(monkeypatch):
    d = Device(device_type="t5", petkit_id=1)

    async def fake_run_cmd_capture(device, device_ip, command, **kwargs):
        assert "/system/user.conf" in command
        assert "/opt/user.conf" in command
        return "STORAGE /system\nSTORAGE /opt\n"

    monkeypatch.setattr("petkit_local.patchers.common.run_cmd_capture", fake_run_cmd_capture)
    assert await detect_patch_storage_dir(d, "127.0.0.1") == "/system"
    assert app_init_wrapper_path(d) == APP_INIT_WRAPPER


async def test_user_conf_probe_uses_opt_when_system_is_absent(monkeypatch):
    d = Device(device_type="d4sh", petkit_id=1)

    async def fake_run_cmd_capture(device, device_ip, command, **kwargs):
        assert "/system/user.conf" in command
        assert "/opt/user.conf" in command
        return "STORAGE /opt\n"

    monkeypatch.setattr("petkit_local.patchers.common.run_cmd_capture", fake_run_cmd_capture)
    assert await detect_patch_storage_dir(d, "127.0.0.1") == "/opt"
    assert app_init_wrapper_path(d) == OPT_APP_INIT_WRAPPER


async def test_user_conf_probe_refuses_to_guess(monkeypatch):
    d = Device(device_type="d4sh", petkit_id=1)

    async def fake_run_cmd_capture(device, device_ip, command, **kwargs):
        return ""

    monkeypatch.setattr("petkit_local.patchers.common.run_cmd_capture", fake_run_cmd_capture)
    try:
        await detect_patch_storage_dir(d, "127.0.0.1")
    except RuntimeError as e:
        assert "neither /system/user.conf nor /opt/user.conf exists" in str(e)
    else:
        raise AssertionError("expected storage probe failure")


def test_patcher_device_files_resolves_bare_names_per_device():
    axera = Device(device_type="d4sh", petkit_id=7)
    set_patch_storage_dir(axera, "/opt")
    ing = Device(device_type="d4sh", petkit_id=8)
    set_patch_storage_dir(ing, "/system")
    info = {"files": ["cloud_patched", "ca_patched.crt"]}
    assert patcher_device_files(info, ing) == [
        "/system/cloud_patched", "/system/ca_patched.crt"]
    assert patcher_device_files(info, axera) == [
        "/opt/cloud_patched", "/opt/ca_patched.crt"]


def test_patcher_device_files_keeps_absolute_paths():
    d = Device(device_type="d4sh", petkit_id=7)
    set_patch_storage_dir(d, "/opt")
    info = {"files": ["/etc/fixed.conf", "/var/lib/fixed.state"]}
    assert patcher_device_files(info, d) == [
        "/etc/fixed.conf", "/var/lib/fixed.state"]


def test_ssh_install_commands_use_system_on_ingenic_layout():
    d = Device(device_type="d4sh", petkit_id=7)
    set_patch_storage_dir(d, "/system")
    cmd = "\n".join(build_install_commands("http://host/patcher", "dropbear-mipsel", d))
    assert "/system/dropbear" in cmd
    assert "/system/authorized_keys" in cmd
    assert "/system/dbkey_ecdsa" in cmd
    assert "/opt/dropbear" not in cmd


def test_ssh_install_commands_use_opt_on_axera():
    d = Device(device_type="d4sh", petkit_id=7)
    set_patch_storage_dir(d, "/opt")
    cmd = "\n".join(build_install_commands("http://host/patcher", "dropbear-armv7", d))
    assert "/opt/dropbear" in cmd
    assert "/opt/authorized_keys" in cmd
    assert "/opt/dbkey_ecdsa" in cmd
    assert "/opt/test_case_root" in cmd
    assert "/system/dropbear" not in cmd


# --- build_wrapper_upload_cmd ---

def test_upload_cmd_writes_wrapper():
    cmd = build_wrapper_upload_cmd({"mqtt"})
    assert APP_INIT_WRAPPER in cmd
    assert "chmod +x" in cmd
    assert "printf" in cmd
    assert "mount --bind /system/ctrl_patched /app/bin/ctrl" in cmd


def test_upload_cmd_escapes_single_quotes():
    cmd = build_wrapper_upload_cmd({"mqtt"})
    # The wrapper itself shouldn't contain single quotes in its content,
    # but if it did the escaping would handle it.
    assert "printf '" in cmd


def test_axera_upload_cmd_writes_opt_wrapper():
    d = Device(device_type="d4sh", petkit_id=7)
    set_patch_storage_dir(d, "/opt")
    cmd = build_wrapper_upload_cmd({"cloud"}, d)
    assert OPT_APP_INIT_WRAPPER in cmd
    assert "mount --bind /opt/cloud_patched /app/bin/cloud" in cmd
    assert "/system/app_init.sh" not in cmd


# --- build_wrapper_remove_cmd ---

def test_remove_cmd():
    cmd = build_wrapper_remove_cmd()
    assert "rm -f" in cmd
    assert APP_INIT_WRAPPER in cmd


def test_axera_remove_cmd_removes_opt_wrapper():
    d = Device(device_type="d4sh", petkit_id=7)
    set_patch_storage_dir(d, "/opt")
    cmd = build_wrapper_remove_cmd(d)
    assert "rm -f" in cmd
    assert OPT_APP_INIT_WRAPPER in cmd
    assert APP_INIT_WRAPPER not in cmd
