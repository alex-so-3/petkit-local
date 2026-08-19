"""dev_feed_get must match the real cloud's responses — 21 of them were
captured through proxy mode from a D4SH on 2026-08-12, and the replay test
below reproduces one to the second."""
import json
import os
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from petkit_local.devices.registry import DeviceRegistry
from petkit_local.http.handlers.feed import (
    _amount_fields,
    _build_latest,
    _compute_next_tick,
    migrate_minute_schedule,
)
from petkit_local.http.server import create_app


@pytest.fixture
def warsaw_tz():
    """The capture came from a device in Europe/Warsaw; countdown values
    depend on where local midnight is."""
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/Warsaw"
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


def _app(device_type="d4sh"):
    reg = DeviceRegistry()
    reg.get_or_create(petkit_id=1, device_type=device_type, serial_number="SN1")
    config = {"api_url": "http://localhost:8080", "bucket_endpoint": "https://localhost:9000"}
    app = create_app(reg, config)
    return app, reg


async def _get_feed(client):
    r = await client.get("/6/d4sh/dev_feed_get",
                         headers={"X-Device": "id=1&nonce=x&timestamp=1&type=d4sh&sign=x"})
    return (await r.json())["result"]


# --- replay of a real cloud response ----------------------------------------
# proxy_http.jsonl, the longest dev_feed_get line: Wed 2026-08-12 18:01:38
# local. Four groups — Thursday's n_46560, Sunday's n_50100/n_71700,
# Wednesday's n_60540 (16:49, already past at capture time) — plus a deferred
# feed for Thursday 17:05. The cloud answered with exactly two latest entries:
# only tomorrow's feeds, Sunday's and next Wednesday's excluded.
CAPTURE_TS = 1786550498.7870133
CAPTURE_SCHEDULE = [
    {"re": "2,3,6,7", "it": []},
    {"re": "5", "it": [{"id": "n_46560", "t": 46560, "a1": 1, "a2": 0}]},
    {"re": "1", "it": [{"id": "n_50100", "t": 50100, "a1": 1, "a2": 0},
                       {"id": "n_71700", "t": 71700, "a1": 1, "a2": 6}]},
    {"re": "4", "it": [{"id": "n_60540", "t": 60540, "a1": 1, "a2": 0}]},
]
CLOUD_LATEST = [
    {"id": "s_20260813_46560", "t": 68061, "a1": 1, "a2": 0},
    {"id": "d_20260813_61500", "t": 83001, "a1": 0, "a2": 1},
]
CLOUD_NEXT_TICK = 83001


def test_replay_matches_cloud_exactly(warsaw_tz):
    deferred_fire = time.mktime((2026, 8, 13, 17, 5, 0, 0, 0, -1))
    feed = {
        "schedule": [dict(g, it=[dict(i) for i in g["it"]])
                     for g in CAPTURE_SCHEDULE],
        "deferred": [{"id": "d_20260813_61500", "a1": 0, "a2": 1,
                      "fire_at": deferred_fire}],
        "v": 2,
    }
    latest = _build_latest(feed, CAPTURE_TS)
    assert latest == CLOUD_LATEST
    assert _compute_next_tick(latest) == CLOUD_NEXT_TICK


def test_far_future_meals_stay_out_of_latest(warsaw_tz):
    """Sunday's meals, four days from the capture moment, are in `schedule`
    but the cloud never put them in `latest` — the window is today+tomorrow."""
    feed = {"schedule": [
        {"re": "1", "it": [{"id": "n_50100", "t": 50100, "a1": 1, "a2": 0}]},
    ], "v": 2}
    assert _build_latest(feed, CAPTURE_TS) == []
    assert _compute_next_tick([]) == 86340


def test_todays_own_passed_meal_is_excluded(warsaw_tz):
    """n_60540 is 16:49 on a Wednesday; at 18:01 the same Wednesday it is
    past, and the cloud did not list next week's instance either."""
    feed = {"schedule": [
        {"re": "4", "it": [{"id": "n_60540", "t": 60540, "a1": 1, "a2": 0}]},
    ], "v": 2}
    assert _build_latest(feed, CAPTURE_TS) == []


# --- single-hopper (D4H/D4H2) amount shape ----------------------------------
# A single-hopper feeder's meal carries one `a`, not the Dual-Hopper's a1/a2.
# The firmware fires from latest[], so latest must repeat `a`: a D4H handed
# a1/a2 runs the feed cycle and dispenses nothing (real_amount 0, err_code 8 —
# codes.FEED_RESULT[8], the mirror of the Dual-Hopper's issue #2).

def test_amount_fields_preserves_the_model_shape():
    assert _amount_fields({"a": 20}) == {"a": 20}
    assert _amount_fields({"a1": 1, "a2": 6}) == {"a1": 1, "a2": 6}
    assert _amount_fields({}) == {"a1": 0, "a2": 0}
    # `a` is the single-hopper key and wins when a payload carries both.
    assert _amount_fields({"a": 5, "a1": 9, "a2": 9}) == {"a": 5}


def test_single_hopper_meal_carries_a_into_latest():
    """The D4H2 bug: a meal with only `a` must reach latest[] as `a`, never
    downgraded to a1/a2. This is the deterministic form of the live check —
    'does latest now carry a:20 at fire time'."""
    now = time.time()
    lt = time.localtime(now)
    secs = lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec
    future_t = (secs + 7200) % 86400
    feed = {"schedule": [{"re": "1,2,3,4,5,6,7", "it": [
        {"id": f"n_{future_t}", "t": future_t, "a": 20},
    ]}], "v": 2}
    latest = _build_latest(feed, now)
    assert latest, "an all-week meal 2h out must appear"
    assert all(e["a"] == 20 and "a1" not in e and "a2" not in e for e in latest)
    assert latest[0]["id"].startswith("s_")
    assert 7100 < latest[0]["t"] < 7300


def test_single_hopper_deferred_carries_a():
    now = time.time()
    feed = {
        "schedule": [{"re": "1,2,3,4,5,6,7", "it": []}],
        "deferred": [{"id": "d_20260819_43200", "a": 20, "fire_at": now + 3600}],
        "v": 2,
    }
    d_entries = [e for e in _build_latest(feed, now) if e["id"].startswith("d_")]
    assert len(d_entries) == 1
    assert d_entries[0]["a"] == 20
    assert "a1" not in d_entries[0] and "a2" not in d_entries[0]


async def test_http_serves_single_hopper_a_end_to_end():
    """What a D4H2 receives on its dev_feed_get poll: schedule[].it[] keeps `a`,
    itemJsonString echoes it, and the recomputed latest[] carries `a` — the
    thing that was zeroed to a1/a2 and made the feed dispense nothing."""
    app, reg = _app(device_type="d4h")
    d = reg.get(1)
    now = time.time()
    lt = time.localtime(now)
    secs = lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec
    future_t = (secs + 7200) % 86400
    d.config["feed_schedule"] = {
        "schedule": [{"re": "1,2,3,4,5,6,7", "it": [
            {"id": f"n_{future_t}", "t": future_t, "a": 20},
        ]}],
        "v": 2,
    }
    async with TestClient(TestServer(app)) as c:
        r = await c.get(
            "/6/d4h/dev_feed_get",
            headers={"X-Device": "id=1&nonce=x&timestamp=1&type=d4h&sign=x"})
        result = (await r.json())["result"]
        meal = result["schedule"][0]["it"][0]
        assert meal == {"id": f"n_{future_t}", "t": future_t, "a": 20}
        assert result["schedule"][0]["itemJsonString"] == \
            f'[{{"a":20,"id":"n_{future_t}","t":{future_t}}}]'
        assert result["latest"]
        assert all(e["a"] == 20 and "a1" not in e for e in result["latest"])


# --- serve-time behaviour ----------------------------------------------------

async def test_empty_schedule_matches_cloud():
    """No schedule: one all-week empty group, latest [], and the cloud's
    86340 constant — never a computed tick."""
    app, reg = _app()
    async with TestClient(TestServer(app)) as c:
        result = await _get_feed(c)
        assert set(result.keys()) == {"schedule", "nextTick", "latest"}
        assert result["schedule"] == [
            {"re": "1,2,3,4,5,6,7", "it": [], "itemJsonString": "[]"}]
        assert result["nextTick"] == 86340
        assert result["latest"] == []


async def test_recurring_meal_appears_with_live_countdown():
    """An all-week meal 2h from now must produce an s_ entry whose t is the
    seconds until it fires, and nextTick must be the last entry's t."""
    app, reg = _app()
    d = reg.get(1)
    now = time.time()
    lt = time.localtime(now)
    secs_since_midnight = lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec
    future_t = (secs_since_midnight + 7200) % 86400
    d.config["feed_schedule"] = {
        "schedule": [{"re": "1,2,3,4,5,6,7", "it": [
            {"id": f"n_{future_t}", "t": future_t, "a1": 1, "a2": 0},
        ]}],
        "v": 2,
    }
    async with TestClient(TestServer(app)) as c:
        result = await _get_feed(c)
        entry = result["latest"][0]
        assert entry["id"].startswith("s_")
        assert entry["a1"] == 1 and entry["a2"] == 0
        assert 7100 < entry["t"] < 7300
        assert result["nextTick"] == max(e["t"] for e in result["latest"])


async def test_itemJsonString_matches_cloud_byte_for_byte():
    """The cloud serializes it[] with keys in alphabetical order and no
    whitespace."""
    app, reg = _app()
    d = reg.get(1)
    d.config["feed_schedule"] = {
        "schedule": [{"re": "5", "it": [
            {"id": "n_46560", "t": 46560, "a1": 1, "a2": 0},
        ]}],
        "v": 2,
    }
    async with TestClient(TestServer(app)) as c:
        result = await _get_feed(c)
        assert result["schedule"][0]["itemJsonString"] == \
            '[{"a1":1,"a2":0,"id":"n_46560","t":46560}]'
        assert json.loads(result["schedule"][0]["itemJsonString"]) == \
            result["schedule"][0]["it"]


async def test_deferred_feed_appears_in_latest():
    app, reg = _app()
    d = reg.get(1)
    future = time.time() + 3600
    d.config["feed_schedule"] = {
        "schedule": [{"re": "1,2,3,4,5,6,7", "it": []}],
        "deferred": [{"id": "d_20260813_61500", "a1": 0, "a2": 1, "fire_at": future}],
        "v": 2,
    }
    async with TestClient(TestServer(app)) as c:
        result = await _get_feed(c)
        d_entries = [e for e in result["latest"] if e["id"].startswith("d_")]
        assert len(d_entries) == 1
        assert 3550 < d_entries[0]["t"] < 3650
        assert result["nextTick"] == d_entries[0]["t"]


async def test_expired_deferred_is_pruned():
    app, reg = _app()
    d = reg.get(1)
    d.config["feed_schedule"] = {
        "schedule": [{"re": "1,2,3,4,5,6,7", "it": []}],
        "deferred": [{"id": "d_20260812_10000", "a1": 1, "a2": 0, "fire_at": time.time() - 60}],
        "v": 2,
    }
    async with TestClient(TestServer(app)) as c:
        result = await _get_feed(c)
        assert result["latest"] == []
        assert d.config["feed_schedule"]["deferred"] == []


async def test_far_future_deferred_is_kept_but_not_listed():
    """A deferred feed past tomorrow stays stored — it must not be pruned —
    but does not enter latest until its day comes into the window."""
    app, reg = _app()
    d = reg.get(1)
    far = time.time() + 5 * 86400
    d.config["feed_schedule"] = {
        "schedule": [{"re": "1,2,3,4,5,6,7", "it": []}],
        "deferred": [{"id": "d_20260817_50000", "a1": 1, "a2": 0, "fire_at": far}],
        "v": 2,
    }
    async with TestClient(TestServer(app)) as c:
        result = await _get_feed(c)
        assert result["latest"] == []
        assert len(d.config["feed_schedule"]["deferred"]) == 1


# --- minute-schedule migration ----------------------------------------------

def test_minute_schedule_is_migrated():
    """A 2.0.0/2.0.1 panel save stored t in minutes; it must come back as
    seconds with cloud-scheme ids, exactly once."""
    feed = {"schedule": [{"re": "1,2,3,4,5,6,7", "it": [
        {"id": "n_1082", "t": 1082, "a1": 1, "a2": 0},
        {"id": 2, "t": 963, "a1": 1, "a2": 0},
    ]}]}
    assert migrate_minute_schedule(feed) is True
    assert feed["schedule"][0]["it"] == [
        {"id": "n_64920", "t": 64920, "a1": 1, "a2": 0},
        {"id": "n_57780", "t": 57780, "a1": 1, "a2": 0},
    ]
    assert feed["v"] == 2
    assert migrate_minute_schedule(feed) is False


def test_stamped_early_morning_schedule_is_not_migrated():
    """Post-fix, a meal before 00:24 has t < 1440 legitimately; the v stamp
    is what keeps the migration off it."""
    feed = {"schedule": [{"re": "1", "it": [
        {"id": "n_1200", "t": 1200, "a1": 1, "a2": 0},
    ]}], "v": 2}
    assert migrate_minute_schedule(feed) is False
    assert feed["schedule"][0]["it"][0]["t"] == 1200


def test_seconds_schedule_without_stamp_is_left_alone():
    """Any meal at or past 00:24 proves the schedule is seconds-based; only
    the stamp is added."""
    feed = {"schedule": [{"re": "1", "it": [
        {"id": "n_46560", "t": 46560, "a1": 1, "a2": 0},
    ]}]}
    assert migrate_minute_schedule(feed) is False
    assert feed["schedule"][0]["it"][0]["t"] == 46560
    assert feed["v"] == 2
