"""Runtime configuration: add-on options, panel overrides and their defaults.

The add-on runs on a plain Python base image with no bashio and no s6-overlay,
so everything the HA supervisor would normally inject has to be read here
instead: `/data/options.json`, the host's LAN IP and the Mosquitto credentials
all come from the Supervisor API. Every read degrades — a missing, damaged or
mistyped option logs and keeps the default, because the add-on failing to start
takes the devices offline with it, while a wrong option only degrades one
feature. The one exception is an explicit `--config` file, which raises.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from petkit_local.utils.coerce import to_bool, to_int
from petkit_local.utils.jsonio import atomic_write_json, read_json

log = logging.getLogger(__name__)

# Runtime-safe settings the web panel may change live (read per-request or via a
# setter, so no restart needed). Persisted to settings_overrides.json and layered
# on top of the add-on options at startup so panel changes survive a restart.
#
# This is the ONLY way proxy mode and capture are configured — they have no
# add-on option and no CLI flag, because both are things you flip mid-session
# while watching a device, not things you restart a container for.
#
# A key missing from this tuple is written by the panel and then silently
# dropped at the next start (`apply_panel_overrides` reads only these names).
PANEL_LIVE_KEYS = (
    "proxy_mode",
    "proxy_upstream",
    "proxy_dns",
    "proxy_block_run_cmd",
    "proxy_block_ota",
    "proxy_block_log_upload",
    "proxy_media_real_oss",
    "proxy_local_cvr_window",
    "proxy_mqtt_bridge",
    "proxy_only",
    "capture",
)
OVERRIDES_FILENAME = "settings_overrides.json"

# main.py resolves the level with getattr(logging, ...), which raises on
# anything else and takes the add-on down before it logs why.
LOG_LEVELS = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG")


def _opt_int(opts: dict[str, Any], key: str, default: int) -> int:
    """Read an integer option, keeping `default` if the value is not one.

    /data/options.json is user-editable, and a bare int() on a typo there
    aborts startup with a traceback that never names the offending option.
    """
    if key not in opts:
        return default
    value = to_int(opts[key], None)
    if value is None:
        log.warning("Option %r is not a number (%r); using %s", key, opts[key], default)
        return default
    return value


def _opt_bool(opts: dict[str, Any], key: str, default: bool) -> bool:
    """Read a boolean option, keeping `default` if the value is not one.

    Not `bool(opts[key])`: the JSON string "false" is truthy, so an option
    written as text would turn the feature ON.
    """
    if key not in opts:
        return default
    value = to_bool(opts[key], None)
    if value is None:
        log.warning("Option %r is not a boolean (%r); using %s", key, opts[key], default)
        return default
    return value


def _supervisor_host_ip() -> str | None:
    """The HA host's LAN IPv4 on its primary interface, via the Supervisor API.

    Used so the device is handed a routable IP instead of an mDNS `.local`
    name, which an embedded PetKit device cannot resolve. Returns None when
    there is no Supervisor (not running as an add-on) or it answers with
    nothing usable — the caller then keeps whatever the options said.
    """
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None
    try:
        req = urllib.request.Request(
            "http://supervisor/network/info",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            ifaces = json.loads(resp.read()).get("data", {}).get("interfaces", [])
    except Exception as e:
        # Worth a line: without it the device is handed whatever api_url the
        # options say, which is exactly the mDNS name it cannot resolve.
        log.warning("Could not auto-detect the HA host IP from the Supervisor: %s", e)
        return None

    ordered = sorted(ifaces, key=lambda i: not i.get("primary"))  # primary first
    for iface in ordered:
        for addr in ((iface.get("ipv4") or {}).get("address") or []):
            ip = addr.split("/")[0]
            if ip and not ip.startswith("127."):
                return ip
    return None


def _supervisor_port_map() -> dict[str, Any]:
    """This add-on's container-port -> host-port mapping, via the Supervisor API.

    `config.yaml` asks for `80/tcp: 80`, but the operator can remap any port in
    the add-on's Network settings, and a device only ever learns where we are
    from the address we hand it. Keys are the `"80/tcp"` form; a value of None
    means the port is not published to the host at all.

    Returns `{}` when there is no Supervisor or it answers with nothing usable,
    which leaves every caller on the mapping `config.yaml` declares.
    """
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return {}
    try:
        req = urllib.request.Request(
            "http://supervisor/addons/self/info",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            network = json.loads(resp.read()).get("data", {}).get("network")
    except Exception as e:
        log.warning("Could not read this add-on's port mapping from the Supervisor "
                    "(%s); assuming the ports config.yaml asks for", e)
        return {}
    return network if isinstance(network, dict) else {}


def _published_port(ports: dict[str, Any], container_port: int) -> int | None:
    """The host port `container_port` is published on, or None if it is not.

    A port the Supervisor did not mention resolves to itself: that is the
    mapping `config.yaml` asks for, and it is what a standalone run has.
    """
    spec = f"{container_port}/tcp"
    if spec not in ports:
        return container_port
    if ports[spec] is None:
        return None
    return to_int(ports[spec], None)


def _auto_api_url(host_ip: str | None, ports: dict[str, Any], http_port: int) -> str:
    """The address to hand devices, on the HOST side of the port mapping.

    `apiServers` in `dev_serverinfo` is the only thing that tells a device where
    this server is, and it is read from outside the container — so it has to
    carry the port the operator actually published, not the one we listen on. A
    portless URL means 80, and an add-on remapped to 8080 then advertises an
    address with nothing behind it (reported by a user running that mapping).

    Empty when the host IP is unknown, which leaves the caller on whatever the
    options said.
    """
    if not host_ip:
        return ""
    port = _published_port(ports, http_port)
    if port is None:
        log.warning("Container port %d/tcp is not published to the host, so no device "
                    "can reach the API. Map it in the add-on's Network settings.",
                    http_port)
        port = 80
    if port != 80:
        log.info("The device API is published on host port %d, so devices will be "
                 "told to use it.", port)
    return f"http://{host_ip}/6/" if port == 80 else f"http://{host_ip}:{port}/6/"


#: Marks that `show_in_sidebar_once` has already run. Lives in the data
#: directory because that is what survives a restart and an update.
SIDEBAR_FLAG_FILENAME = "sidebar_offered.flag"


def show_in_sidebar_once(data_dir: str) -> None:
    """Put the panel in Home Assistant's sidebar, the first time only.

    "Show in sidebar" is not something an add-on can declare. It is per-install
    state the Supervisor keeps (`SCHEMA_APP_USER`, default False) and only its
    API writes; there is no `config.yaml` key for it, and nothing turns it on at
    install time. So a fresh install hides the panel that IS this add-on's
    interface, and the user has to find a toggle to see it at all.

    Done exactly once, recorded by a file in `data_dir`. That distinction is the
    whole design: setting it on every start would be an add-on that reinstates
    itself in the sidebar every time someone removes it, which is worse than the
    problem it solves. After the first run the choice is the operator's.

    Never raises. Failing to tidy the sidebar must not stop the add-on starting.
    """
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return
    flag = Path(data_dir) / SIDEBAR_FLAG_FILENAME
    if flag.exists():
        return
    try:
        req = urllib.request.Request(
            "http://supervisor/addons/self/options",
            data=json.dumps({"ingress_panel": True}).encode(),
            method="POST",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception as e:
        log.debug("Could not add the panel to the sidebar: %s", e)
        return
    # Written only after the call succeeded, so a Supervisor that was briefly
    # unreachable gets another try rather than silently never showing the panel.
    try:
        flag.write_text("The panel was added to the sidebar once, on first run.\n"
                        "Delete this file to have it offered again.\n")
    except OSError:
        pass
    log.info("Added the web panel to Home Assistant's sidebar (first run only)")


@dataclass
class Config:
    """Every tunable the add-on has, with a working default for each.

    A default-constructed `Config` is a valid one — nothing here is required —
    so a broken options file degrades to a running add-on rather than a crash
    loop. Only the keys in `PANEL_LIVE_KEYS` may be changed while running; the
    rest are read once at startup and a change to them needs a restart.
    """

    http_port: int = 80
    mqtt_port: int = 1883
    #: What a device is told to call, and the only thing that tells it where we
    #: are. Empty means "auto-detect", which the add-on path resolves from the
    #: Supervisor's host IP and published port (`_auto_api_url`); a standalone
    #: run has no such source and needs `--api-url`. An explicit value is used
    #: verbatim, port and all.
    api_url: str = ""
    data_dir: str = "/data"
    log_level: str = "INFO"

    bucket_port: int = 9000
    #: Where the device is told to upload its photos and video. Empty means
    #: "derive it from `api_url`" — see `resolve_bucket_endpoint`, which runs
    #: after the CLI flags have been applied. Set it explicitly when the device
    #: must upload through a different host, port or TLS-terminating proxy than
    #: it calls the API on.
    bucket_endpoint: str = ""

    # Device-facing MQTT TLS (Aliyun securemode=2). The plain listener stays up
    # for the internal bridge; a TLS listener is added on mqtt_tls_port.
    mqtt_tls: bool = False
    mqtt_tls_port: int = 443
    mqtt_cert: str = ""
    mqtt_key: str = ""
    # Enforce Aliyun HMAC sign. Default off = accept-all (like the reference
    # broker), so an algorithm/sign nuance never blocks a real device.
    mqtt_strict_auth: bool = False
    # Hand ESP32 models their Aliyun broker name as mqttHost instead of the
    # host derived from api_url. Needs DNS and a certificate for that name;
    # see `http/handlers/iot_device_info.py::mqtt_host_for`.
    esp32_aliyun_mqtt_host: bool = False

    # Seconds without a heartbeat or state report before a device is marked
    # offline (availability -> "offline" in HA).
    offline_timeout: int = 180

    # When enabled, raw HTTP state reports and MQTT messages are appended to
    # JSONL files under {data_dir}/capture for reverse-engineering / parser tuning.
    # Panel-only (see PANEL_LIVE_KEYS).
    capture: bool = False

    # Proxy mode: forward every device request to the real PetKit cloud and
    # answer with its reply, redacted (`http/redact/`). All panel-only.
    proxy_mode: bool = False
    #: A key of `http/proxy.py::UPSTREAM_PRESETS`, a full URL, or "" for
    #: `proxy.DEFAULT_UPSTREAM`.
    proxy_upstream: str = ""
    #: DNS server for upstream lookups only, `1.1.1.1` or `10.0.0.1:5353`.
    #: Empty uses the system resolver. Needed when this LAN's own DNS answers
    #: PetKit's names with this add-on, which is one of the two documented ways
    #: to redirect a device and silently defeats proxy mode (`http/dns.py`).
    proxy_dns: str = ""
    #: Strip shell commands from an upstream reply. Off is a genuinely bad idea
    #: and exists only so a capture can prove what the cloud sent.
    proxy_block_run_cmd: bool = True
    #: Answer the OTA endpoints locally and drop firmware images found
    #: elsewhere. A bad answer there is the one way this server could brick a
    #: device (`handlers/stubs.py::handle_ota_check`).
    proxy_block_ota: bool = True
    #: Withhold the STS token the device needs to upload its own debug log to
    #: PetKit. That log is a full request transcript including this add-on's LAN
    #: address, so it tells the cloud exactly how the device was taken over.
    proxy_block_log_upload: bool = True
    #: Let the device upload media to PetKit's OSS instead of our bucket. Off by
    #: default because turning proxy mode on would otherwise silently stop every
    #: recording from landing locally.
    proxy_media_real_oss: bool = False
    #: Answer a camera with OUR standing cloud-storage window instead of
    #: the upstream's. Off by default -- see `redact/rules.py`.
    proxy_local_cvr_window: bool = False
    #: Comma-separated endpoint names to forward, e.g.
    #: "dev_device_info,dev_multi_config". Empty means every endpoint, which is
    #: normal operation. A non-empty list answers everything else locally, which
    #: is how you bisect: the firmware can react badly to a reply that is
    #: perfectly valid but simply not the one we usually send, and this is the
    #: only way to find out which one.
    proxy_only: str = ""
    #: Bridge the device's MQTT session to the real Aliyun broker
    #: (`mqtt/upstream.py`). Only has an effect while proxy mode is on and the
    #: real credentials have been learned from a proxied dev_iot_device_info.
    proxy_mqtt_bridge: bool = True

    ha_mqtt_host: str = "localhost"
    ha_mqtt_port: int = 1883
    ha_mqtt_user: str = ""
    ha_mqtt_pass: str = ""
    ha_discovery_prefix: str = "homeassistant"

    # Web panel served through HA Ingress (internal port). There is deliberately
    # no second HTTPS port: one existed (8098, self-signed) so Web Bluetooth had
    # a top-level secure context, but it served this entire API — device
    # settings, commands, pet records, the on-device patchers — to the LAN with
    # no authentication. Provisioning now asks the operator for a real
    # certificate in front of this port instead.
    web_port: int = 8099

    @property
    def capture_dir(self) -> str:
        """Where `utils/capture.py` appends its JSONL files."""
        return f"{self.data_dir}/capture"

    @property
    def device_log_dir(self) -> str:
        """Where a device's own uploaded debug logs are stored.

        Under `data_dir` rather than the media share, for the reason given at
        the call site in `main.py`: these are not media, and the media
        pipeline's raw-file lookup must never be able to see one.
        """
        return f"{self.data_dir}/devicelogs"

    @property
    def overrides_path(self) -> Path:
        """Where the panel's live setting changes are persisted."""
        return Path(self.data_dir) / OVERRIDES_FILENAME

    def resolve_bucket_endpoint(self) -> None:
        """Point the media bucket at an address the DEVICE can reach.

        `dev_oss_sts_info_new_v2` hands the device a URL it will upload photos
        and video to, on its own, from the other side of the network. Without
        this a standalone run had nothing to put there and fell back to
        `https://localhost:9000` — which resolves, on the device, to the device.
        Reported by a user running docker-compose: every upload address in the
        STS response read `localhost`, so nothing could ever arrive.

        `api_url` is the right source and the only one available: it is the
        address the operator configured for the device to call, and the request
        being answered arrived on it. Same reasoning as
        `handlers/iot_device_info.py::self_mqtt_host`, which picks the MQTT
        broker address the same way and for the same reason.

        Does nothing when the endpoint is already set — the add-on path fills it
        from the Supervisor's host IP — and nothing when `api_url` has no host
        to give, which leaves the empty value that makes `to_oss_sts` and
        `to_log_upload_token` answer with no upload target at all. That is the
        honest outcome: better than naming an address that cannot work.
        """
        if self.bucket_endpoint:
            return
        host = urlparse(self.api_url).hostname
        if not host:
            log.warning("No api_url host, so devices cannot be told where to upload "
                        "media. Set --api-url to an address the DEVICE can reach.")
            return
        self.bucket_endpoint = f"https://{host}:{self.bucket_port}"

    def apply_panel_overrides(self) -> None:
        """Layer the panel's saved runtime overrides on top of the add-on options.

        Only `PANEL_LIVE_KEYS` are read back, so a stale or hand-edited
        overrides file can never resurrect a setting the panel is not allowed to
        change live. This is what makes a toggle flipped in the web UI (proxy,
        capture) survive a restart.
        """
        data = read_json(self.overrides_path, {})
        if not isinstance(data, dict):
            log.warning("Ignoring %s: not a JSON object", self.overrides_path)
            return
        for k in PANEL_LIVE_KEYS:
            if k in data:
                setattr(self, k, data[k])

    def to_app_config(self) -> dict[str, Any]:
        """The subset the aiohttp app stores in `app["config"]`.

        A plain dict rather than the `Config` itself, so a request handler can
        only read what the device-facing side legitimately needs (URLs, proxy
        settings, paths) and never the HA broker password.
        """
        return {
            "api_url": self.api_url,
            "mqtt_port": self.mqtt_port,
            "esp32_aliyun_mqtt_host": self.esp32_aliyun_mqtt_host,
            "proxy_mode": self.proxy_mode,
            "proxy_upstream": self.proxy_upstream,
            "proxy_dns": self.proxy_dns,
            "proxy_block_run_cmd": self.proxy_block_run_cmd,
            "proxy_block_ota": self.proxy_block_ota,
            "proxy_block_log_upload": self.proxy_block_log_upload,
            "proxy_media_real_oss": self.proxy_media_real_oss,
            "proxy_local_cvr_window": self.proxy_local_cvr_window,
            "proxy_mqtt_bridge": self.proxy_mqtt_bridge,
            "proxy_only": self.proxy_only,
            "capture": self.capture,
            "capture_dir": self.capture_dir,
            "bucket_endpoint": self.bucket_endpoint,
            "data_dir": self.data_dir,
        }

    @classmethod
    def from_file(cls, path: str | Path) -> Config:
        """Load an explicit config file.

        Unlike the add-on options this path is only used when the operator
        passed --config, so a damaged file raises instead of silently falling
        back to defaults they did not ask for.

        Raises:
            ValueError: if the file exists but is not readable JSON. The
                message names the file, which json's own error does not.
        """
        p = Path(path)
        if not p.exists():
            return cls()
        try:
            data = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError) as e:
            raise ValueError(f"invalid config file {p}: {e}") from e
        if not isinstance(data, dict):
            raise ValueError(f"invalid config file {p}: expected a JSON object")
        c = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        c.apply_panel_overrides()
        return c

    @classmethod
    def from_ha_addon(cls) -> Config:
        """Self-configure as a HA add-on, from `/data/options.json` and the Supervisor.

        Replaces what bashio would normally do, so the add-on can run on a plain
        python base image with no s6-overlay. Nothing here raises: every source
        (the options file, the host IP, the MQTT service, the broker override
        file) independently degrades to the dataclass default, because a config
        error must not stop the add-on from serving the devices.
        """
        opts = read_json(Path("/data/options.json"), {})
        if not isinstance(opts, dict):
            log.warning("/data/options.json is not a JSON object; using defaults")
            opts = {}

        c = cls()
        c.http_port = 80
        c.mqtt_port = 1883
        c.data_dir = "/data"

        # Device-facing host: an embedded PetKit device can't resolve mDNS
        # (`.local`), so auto-detect the HA host's LAN IP when the option is
        # empty or an mDNS name. An explicit non-.local value is respected, port
        # and all — it is the escape hatch for anything the mapping cannot say.
        host_ip = _supervisor_host_ip()
        ports = _supervisor_port_map()

        api_opt = (opts.get("api_url") or "").strip()
        if not api_opt or ".local" in api_opt:
            auto = _auto_api_url(host_ip, ports, c.http_port)
            c.api_url = auto or api_opt or c.api_url
        else:
            c.api_url = api_opt

        # There is deliberately no global mqtt_host option: the host handed to a
        # device is derived per request from the URL it reached us on
        # (`Device.resolve_mqtt_host`), so one add-on serves devices that see us
        # under different addresses.
        #
        # The MQTT port cannot be handed over at all: the firmware dials it from
        # its own build and no response field overrides that, so a remap here can
        # only be reported.
        if c.mqtt_tls and _published_port(ports, c.mqtt_tls_port) != c.mqtt_tls_port:
            log.warning("Container port %d/tcp is not published on host port %d, and a "
                        "device dials that port from firmware. TLS MQTT will not "
                        "connect until the mapping matches.", c.mqtt_tls_port,
                        c.mqtt_tls_port)

        bucket_opt = (opts.get("bucket_endpoint") or "").strip()
        if bucket_opt:
            c.bucket_endpoint = bucket_opt
        elif host_ip:
            bucket_port = _published_port(ports, c.bucket_port)
            if bucket_port is None:
                log.warning("Container port %d/tcp is not published to the host, so no "
                            "device can upload media. Map it in the add-on's Network "
                            "settings, or set the bucket_endpoint option.", c.bucket_port)
                bucket_port = c.bucket_port
            c.bucket_endpoint = f"https://{host_ip}:{bucket_port}"
        level = str(opts.get("log_level", "INFO")).upper()
        if level not in LOG_LEVELS:
            log.warning("Option 'log_level' is not one of %s (%r); using INFO",
                        ", ".join(LOG_LEVELS), opts.get("log_level"))
            level = "INFO"
        c.log_level = level
        c.offline_timeout = _opt_int(opts, "offline_timeout", c.offline_timeout)
        c.mqtt_tls = _opt_bool(opts, "mqtt_tls", c.mqtt_tls)
        c.mqtt_tls_port = _opt_int(opts, "mqtt_tls_port", c.mqtt_tls_port)
        c.mqtt_strict_auth = _opt_bool(opts, "mqtt_strict_auth", c.mqtt_strict_auth)
        c.esp32_aliyun_mqtt_host = _opt_bool(
            opts, "esp32_aliyun_mqtt_host", c.esp32_aliyun_mqtt_host)

        # `capture` and every `proxy_*` key are deliberately NOT read from the
        # options file: they are debugging switches you flip while watching a
        # device, and going through the Supervisor means editing YAML and
        # restarting the add-on to change one. They live in the panel's Live
        # settings and are applied here — before any return below.
        c.apply_panel_overrides()

        # HA MQTT broker (for publishing entities to HA). We do NOT assume MQTT
        # exists — if nothing is configured, HA publishing stays disabled (the
        # device side still works). Priority:
        #   1. add-on options (ha_mqtt_host/...) — configure your own broker
        #   2. Supervisor `mqtt` service (auto, only if the Mosquitto add-on runs)
        #   3. /data/ha_broker.json override (fallback when the manifest can't
        #      take new options yet; {"host","port","username","password"})
        #   else: empty -> HA publishing disabled, no error spam
        c.ha_mqtt_host = ""
        if opts.get("ha_mqtt_host"):
            c.ha_mqtt_host = str(opts["ha_mqtt_host"])
            c.ha_mqtt_port = _opt_int(opts, "ha_mqtt_port", 1883)
            c.ha_mqtt_user = str(opts.get("ha_mqtt_user", "") or "")
            c.ha_mqtt_pass = str(opts.get("ha_mqtt_pass", "") or "")
            return c

        broker = read_json(Path("/data/ha_broker.json"), None)
        if isinstance(broker, dict):
            c.ha_mqtt_host = str(broker.get("host", c.ha_mqtt_host) or "")
            c.ha_mqtt_port = _opt_int(broker, "port", 1883)
            c.ha_mqtt_user = str(broker.get("username", "") or "")
            c.ha_mqtt_pass = str(broker.get("password", "") or "")
            return c

        token = os.environ.get("SUPERVISOR_TOKEN")
        if token:
            try:
                req = urllib.request.Request(
                    "http://supervisor/services/mqtt",
                    headers={"Authorization": f"Bearer {token}"},
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read()).get("data", {})
                c.ha_mqtt_host = str(data.get("host", "") or "")
                c.ha_mqtt_port = _opt_int(data, "port", 1883)
                c.ha_mqtt_user = str(data.get("username", "") or "")
                c.ha_mqtt_pass = str(data.get("password", "") or "")
            except Exception as e:
                # Expected whenever the Mosquitto add-on is not installed, so
                # this stays at INFO — but silence made "why is HA publishing
                # off?" unanswerable from the log alone.
                log.info("No MQTT service from the Supervisor (%s); HA publishing stays "
                         "disabled unless ha_mqtt_host is set", e)
        return c

    def save(self, path: str | Path) -> None:
        """Write the whole config to `path`, atomically.

        Includes the HA broker password, so the destination must not be
        anywhere the panel or the device-facing HTTP server can serve from.
        """
        atomic_write_json(path, asdict(self))
