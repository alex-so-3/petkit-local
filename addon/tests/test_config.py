import json
import os
import tempfile
from pathlib import Path

import petkit_local.config as config_mod
from petkit_local.config import (
    Config, OVERRIDES_FILENAME, PANEL_LIVE_KEYS, _opt_bool, _opt_int,
)


def _from_ha_addon_with(options: dict, broker: dict | None = None,
                        host_ip: str | None = None,
                        ports: dict | None = None) -> Config:
    """Run Config.from_ha_addon() against fake /data files.

    The real one reads absolute paths and talks to the Supervisor, neither of
    which exists here, so all three are stubbed out for the duration of the
    call. `ports` is the Supervisor's own `network` map (`{"80/tcp": 8080}`),
    and None stands for "the Supervisor said nothing".
    """
    def fake_read_json(path, default):
        name = Path(path).name
        if name == "options.json":
            return options
        if name == "ha_broker.json" and broker is not None:
            return broker
        return default

    real_read_json = config_mod.read_json
    real_host_ip = config_mod._supervisor_host_ip
    real_port_map = config_mod._supervisor_port_map
    token = os.environ.pop("SUPERVISOR_TOKEN", None)
    config_mod.read_json = fake_read_json
    config_mod._supervisor_host_ip = lambda: host_ip
    config_mod._supervisor_port_map = lambda: ports or {}
    try:
        return Config.from_ha_addon()
    finally:
        config_mod.read_json = real_read_json
        config_mod._supervisor_host_ip = real_host_ip
        config_mod._supervisor_port_map = real_port_map
        if token is not None:
            os.environ["SUPERVISOR_TOKEN"] = token


# --- option coercion (FIX 4) ------------------------------------------------

def test_opt_int_keeps_the_default_for_garbage():
    assert _opt_int({"offline_timeout": "soon"}, "offline_timeout", 180) == 180
    assert _opt_int({"offline_timeout": None}, "offline_timeout", 180) == 180
    assert _opt_int({"offline_timeout": ""}, "offline_timeout", 180) == 180
    assert _opt_int({}, "offline_timeout", 180) == 180


def test_opt_int_accepts_the_forms_options_json_can_hold():
    assert _opt_int({"n": 300}, "n", 180) == 300
    assert _opt_int({"n": "300"}, "n", 180) == 300
    assert _opt_int({"n": 300.0}, "n", 180) == 300


def test_opt_bool_rejects_the_string_false():
    # bool("false") is True, which used to turn the option ON.
    assert _opt_bool({"mqtt_tls": "false"}, "mqtt_tls", False) is False
    assert _opt_bool({"mqtt_tls": "OFF"}, "mqtt_tls", True) is False
    assert _opt_bool({"mqtt_tls": "true"}, "mqtt_tls", False) is True
    assert _opt_bool({"mqtt_tls": "maybe"}, "mqtt_tls", False) is False
    assert _opt_bool({}, "mqtt_tls", True) is True


def test_from_ha_addon_survives_a_garbage_offline_timeout():
    c = _from_ha_addon_with({"offline_timeout": "later"})
    assert c.offline_timeout == 180


def test_from_ha_addon_survives_every_option_being_garbage():
    c = _from_ha_addon_with({
        "offline_timeout": "later",
        "mqtt_tls_port": [],
        "mqtt_tls": "maybe",
        "mqtt_strict_auth": {},
        "log_level": "LOUD",
        "ha_mqtt_host": "10.0.0.2",
        "ha_mqtt_port": "not a port",
    })
    assert c.offline_timeout == 180
    assert c.mqtt_tls_port == 443
    assert c.mqtt_tls is False
    assert c.capture is False
    assert c.log_level == "INFO"
    assert c.ha_mqtt_host == "10.0.0.2"
    assert c.ha_mqtt_port == 1883


def test_from_ha_addon_applies_valid_options():
    c = _from_ha_addon_with({
        "offline_timeout": "300",
        "mqtt_tls": True,
        "mqtt_tls_port": 8883,
        "log_level": "debug",
    })
    assert c.offline_timeout == 300
    assert c.mqtt_tls is True
    assert c.mqtt_tls_port == 8883
    assert c.log_level == "DEBUG"  # main.py resolves this with getattr(logging, ...)


def test_esp32_aliyun_mqtt_host_option_reaches_the_app_config():
    assert _from_ha_addon_with({}).to_app_config()["esp32_aliyun_mqtt_host"] is False
    c = _from_ha_addon_with({"esp32_aliyun_mqtt_host": True})
    assert c.esp32_aliyun_mqtt_host is True
    assert c.to_app_config()["esp32_aliyun_mqtt_host"] is True
    assert _from_ha_addon_with({"esp32_aliyun_mqtt_host": "off"}).esp32_aliyun_mqtt_host is False


def test_proxy_and_capture_are_not_add_on_options():
    """They are panel-only now: debugging switches you flip while watching a
    device, not something worth restarting the container for. An options file
    left over from an older install must not resurrect them."""
    c = _from_ha_addon_with({"capture": True, "proxy": True,
                             "proxy_upstream": "https://api.eu-pet.com"})
    assert c.capture is False
    assert c.proxy_mode is False
    assert c.proxy_upstream == ""


def test_from_ha_addon_tolerates_a_non_object_options_file():
    c = _from_ha_addon_with(["not", "an", "object"])
    assert c.offline_timeout == 180


def test_from_ha_addon_coerces_the_ha_broker_override():
    c = _from_ha_addon_with({}, broker={"host": "10.0.0.9", "port": "1884",
                                        "username": "u", "password": "p"})
    assert c.ha_mqtt_host == "10.0.0.9"
    assert c.ha_mqtt_port == 1884
    assert c.ha_mqtt_user == "u"


def test_from_ha_addon_ignores_a_broken_ha_broker_port():
    c = _from_ha_addon_with({}, broker={"host": "10.0.0.9", "port": "kaboom"})
    assert c.ha_mqtt_host == "10.0.0.9"
    assert c.ha_mqtt_port == 1883


# --- the address devices are handed -----------------------------------------

def test_the_advertised_api_url_carries_the_published_host_port():
    """The reported bug: remapping 80/tcp to 8080 left `apiServers` pointing at
    port 80, where nothing listens."""
    c = _from_ha_addon_with({}, host_ip="192.168.1.5",
                            ports={"80/tcp": 8080, "9000/tcp": 9000})
    assert c.api_url == "http://192.168.1.5:8080/6/"


def test_the_default_mapping_stays_portless():
    c = _from_ha_addon_with({}, host_ip="192.168.1.5",
                            ports={"80/tcp": 80, "9000/tcp": 9000})
    assert c.api_url == "http://192.168.1.5/6/"


def test_a_supervisor_that_says_nothing_leaves_the_declared_ports():
    c = _from_ha_addon_with({}, host_ip="192.168.1.5")
    assert c.api_url == "http://192.168.1.5/6/"
    assert c.bucket_endpoint == "https://192.168.1.5:9000"


def test_an_unpublished_api_port_still_yields_a_usable_url():
    """Nothing can reach us, but the URL must stay well-formed — the warning is
    what tells the operator, not a broken config."""
    c = _from_ha_addon_with({}, host_ip="192.168.1.5", ports={"80/tcp": None})
    assert c.api_url == "http://192.168.1.5/6/"


def test_an_explicit_api_url_is_used_verbatim():
    c = _from_ha_addon_with({"api_url": "http://10.0.0.2:9999/6/"},
                            host_ip="192.168.1.5", ports={"80/tcp": 8080})
    assert c.api_url == "http://10.0.0.2:9999/6/"


def test_an_mdns_api_url_is_replaced_by_the_detected_address():
    c = _from_ha_addon_with({"api_url": "http://homeassistant.local/6/"},
                            host_ip="192.168.1.5", ports={"80/tcp": 8080})
    assert c.api_url == "http://192.168.1.5:8080/6/"


def test_the_bucket_endpoint_follows_its_published_port():
    c = _from_ha_addon_with({}, host_ip="192.168.1.5",
                            ports={"80/tcp": 80, "9000/tcp": 19000})
    assert c.bucket_endpoint == "https://192.168.1.5:19000"


def test_an_explicit_bucket_endpoint_wins():
    c = _from_ha_addon_with({"bucket_endpoint": "https://uploads.example:443"},
                            host_ip="192.168.1.5", ports={"9000/tcp": 19000})
    assert c.bucket_endpoint == "https://uploads.example:443"


def test_a_garbage_published_port_reads_as_unpublished():
    c = _from_ha_addon_with({}, host_ip="192.168.1.5", ports={"80/tcp": "eighty"})
    assert c.api_url == "http://192.168.1.5/6/"


# --- panel overrides --------------------------------------------------------

def test_apply_panel_overrides_applies_live_keys():
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / OVERRIDES_FILENAME).write_text(
            json.dumps({"proxy_mode": True, "capture": True, "http_port": 9999}))
        c = Config(data_dir=tmp)
        c.apply_panel_overrides()

        assert c.proxy_mode is True
        assert c.capture is True
        assert c.http_port == 80  # not a live key, so not overridable


def test_apply_panel_overrides_ignores_a_corrupt_file():
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / OVERRIDES_FILENAME).write_text("{ truncated")
        c = Config(data_dir=tmp)
        c.apply_panel_overrides()

        assert c.proxy_mode is False


def test_apply_panel_overrides_ignores_a_missing_file():
    with tempfile.TemporaryDirectory() as tmp:
        Config(data_dir=tmp).apply_panel_overrides()  # must not raise


def test_every_live_key_round_trips_through_the_overrides_file():
    """The panel is the only control surface for these, so a key it can write
    and `apply_panel_overrides` cannot read back is a setting that silently
    resets at every restart."""
    non_default = {
        "proxy_mode": True,
        "proxy_upstream": "petkit-cn",
        "proxy_dns": "1.1.1.1",
        "proxy_block_run_cmd": False,
        "proxy_block_ota": False,
        "proxy_block_log_upload": False,
        "proxy_media_real_oss": True,
        "proxy_local_cvr_window": True,
        "proxy_mqtt_bridge": False,
        "proxy_only": "dev_device_info",
        "capture": True,
    }
    assert set(non_default) == set(PANEL_LIVE_KEYS)

    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / OVERRIDES_FILENAME).write_text(json.dumps(non_default))
        c = Config(data_dir=tmp)
        c.apply_panel_overrides()

        for key, value in non_default.items():
            assert getattr(c, key) == value, key


def test_every_live_key_reaches_the_device_facing_config():
    """`to_app_config` is the dict the handlers and the middleware read, and the
    same object the panel mutates — a live key missing from it is a toggle with
    no effect until restart."""
    app_config = Config().to_app_config()
    for key in PANEL_LIVE_KEYS:
        assert key in app_config, key


# --- persistence (FIX 1) ----------------------------------------------------

def test_save_is_atomic_and_roundtrips():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        c = Config(http_port=8080, offline_timeout=240, data_dir=tmp)
        c.save(path)

        assert sorted(os.listdir(tmp)) == ["config.json"], "temp file was left behind"
        loaded = Config.from_file(path)
        assert loaded.http_port == 8080
        assert loaded.offline_timeout == 240


def test_save_keeps_the_previous_file_when_the_rename_fails():
    import petkit_local.utils.jsonio as jsonio

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        Config(http_port=8080, data_dir=tmp).save(path)

        def failing_replace(src, dst):
            raise OSError("simulated container kill")

        real_replace = jsonio.os.replace
        jsonio.os.replace = failing_replace
        try:
            Config(http_port=9999, data_dir=tmp).save(path)
        except OSError:
            pass
        finally:
            jsonio.os.replace = real_replace

        assert sorted(os.listdir(tmp)) == ["config.json"]
        assert Config.from_file(path).http_port == 8080


def test_from_file_missing_returns_defaults():
    with tempfile.TemporaryDirectory() as tmp:
        assert Config.from_file(Path(tmp) / "nope.json").http_port == 80


def test_from_file_names_the_file_on_bad_json():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "config.json"
        path.write_text("{ truncated")
        raised = ""
        try:
            Config.from_file(path)
        except ValueError as e:
            raised = str(e)

        assert "config.json" in raised, "the error must name the file it choked on"
