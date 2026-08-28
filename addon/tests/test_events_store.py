import asyncio
import sqlite3
import tempfile
import time
from pathlib import Path

from petkit_local.events.store import EventStore
from petkit_local.utils.timeutil import local_day_start


async def test_upsert_event_dedups_by_event_uid(event_store: EventStore):
    row = {"event_uid": "e1", "device_id": 1, "event_type": "pet_out",
           "event_kind": "toilet_visit", "ts": 100.0}
    id1 = await event_store.upsert_event(row)
    row2 = {**row, "ts": 101.0}
    id2 = await event_store.upsert_event(row2)
    assert id1 == id2
    ev = await event_store.get_event(id1)
    assert ev["ts"] == 101.0


async def test_upsert_event_without_uid_always_inserts(event_store: EventStore):
    id1 = await event_store.upsert_event({"device_id": 1, "event_type": "move_detect", "ts": 1.0})
    id2 = await event_store.upsert_event({"device_id": 1, "event_type": "move_detect", "ts": 2.0})
    assert id1 != id2


async def test_upsert_event_drops_unknown_keys(event_store: EventStore):
    eid = await event_store.upsert_event({"device_id": 1, "event_type": "a", "ts": 1.0,
                                          "some_future_device_field": "boom"})
    assert (await event_store.get_event(eid))["event_type"] == "a"


async def test_prune_events_by_age(event_store: EventStore):
    await event_store.upsert_event({"device_id": 1, "event_type": "a", "ts": 10.0})
    await event_store.upsert_event({"device_id": 1, "event_type": "b", "ts": 1000.0})
    removed = await event_store.prune_events(before_ts=500.0)
    assert removed == 1
    remaining = await event_store.query_timeline(device_id=1)
    assert len(remaining) == 1
    assert remaining[0]["event_type"] == "b"


async def test_update_event_fields_commits_without_a_transaction(event_store: EventStore):
    eid = await event_store.upsert_event({"device_id": 1, "event_type": "5", "ts": 10.0})
    assert await event_store.update_event_fields(eid, event_kind="cleaning") is True
    # Ignores keys that aren't writable columns, and says so.
    assert await event_store.update_event_fields(eid, nonsense="x") is False
    await event_store.close()

    reopened = EventStore(event_store._path)
    assert (await reopened.get_event(eid))["event_kind"] == "cleaning"


async def test_transaction_batches_writes_into_one_commit(event_store: EventStore):
    ids = [await event_store.upsert_event({"device_id": 1, "event_type": "a", "ts": float(i)})
           for i in range(3)]
    async with event_store.transaction():
        for eid in ids:
            await event_store.update_event_fields(eid, event_kind="cleaning")
    for eid in ids:
        assert (await event_store.get_event(eid))["event_kind"] == "cleaning"


async def test_transaction_rolls_back_on_error(event_store: EventStore):
    eid = await event_store.upsert_event({"device_id": 1, "event_type": "a", "ts": 1.0})
    try:
        async with event_store.transaction():
            await event_store.update_event_fields(eid, event_kind="cleaning")
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert (await event_store.get_event(eid))["event_kind"] is None
    # The write lock survives a failed batch.
    await event_store.update_event_fields(eid, event_kind="other")
    assert (await event_store.get_event(eid))["event_kind"] == "other"


async def test_upsert_media_dedups_by_file_id_and_supports_partial_update(event_store: EventStore):
    id1 = await event_store.upsert_media({
        "file_id": "f1", "device_id": 1, "related_event": "r1",
        "category": "eventImage", "status": "pending",
    })
    id2 = await event_store.upsert_media({"file_id": "f1", "device_id": 1, "status": "ready",
                                          "media_path": "/media/petkit/x.jpg"})
    assert id1 == id2
    m = await event_store.get_media_by_file_id("f1")
    assert m["status"] == "ready"
    assert m["media_path"] == "/media/petkit/x.jpg"
    assert m["related_event"] == "r1"  # untouched by the partial update


async def test_upsert_media_requires_file_id(event_store: EventStore):
    try:
        await event_store.upsert_media({"device_id": 1})
        assert False, "expected ValueError"
    except ValueError:
        pass


async def test_upsert_media_honours_an_explicit_created_at(event_store: EventStore):
    await event_store.upsert_media({"file_id": "f1", "device_id": 1, "status": "ready",
                                    "category": "fullVideo", "created_at": 1000.0})
    assert (await event_store.get_media_by_file_id("f1"))["created_at"] == 1000.0


async def test_query_timeline_attaches_media_by_related_event(event_store: EventStore):
    await event_store.upsert_event({"event_uid": "e1", "related_event": "r1", "device_id": 1,
                                    "event_type": "pet_out", "event_kind": "toilet_visit",
                                    "ts": 10.0})
    await event_store.upsert_media({"file_id": "f1", "device_id": 1, "related_event": "r1",
                                    "category": "highLight", "status": "ready"})
    await event_store.upsert_media({"file_id": "f2", "device_id": 1, "related_event": "other",
                                    "category": "eventImage", "status": "ready"})
    rows = await event_store.query_timeline(device_id=1)
    assert len(rows) == 1
    assert len(rows[0]["media"]) == 1
    assert rows[0]["media"][0]["file_id"] == "f1"


async def test_query_timeline_filters_by_device_and_window(event_store: EventStore):
    await event_store.upsert_event({"device_id": 1, "event_type": "a", "ts": 100.0})
    await event_store.upsert_event({"device_id": 2, "event_type": "b", "ts": 100.0})
    await event_store.upsert_event({"device_id": 1, "event_type": "c", "ts": 500.0})
    rows = await event_store.query_timeline(device_id=1, start_ts=0, end_ts=200)
    assert len(rows) == 1
    assert rows[0]["event_type"] == "a"


async def test_media_for_retention_and_delete(event_store: EventStore):
    await event_store.upsert_media({"file_id": "f1", "device_id": 1, "category": "fullVideo",
                                    "status": "ready"})
    await event_store.upsert_media({"file_id": "f2", "device_id": 1, "category": "fullVideo",
                                    "status": "pending"})
    ready = await event_store.media_for_retention("fullVideo")
    assert len(ready) == 1
    assert ready[0]["file_id"] == "f1"
    await event_store.delete_media(ready[0]["id"])
    assert await event_store.media_for_retention("fullVideo") == []


async def test_stitch_candidates_group_by_category_and_quiescence(event_store: EventStore):
    for i in range(3):
        await event_store.upsert_media({"file_id": f"main{i}", "device_id": 1,
                                        "related_event": "r1",
                                        "category": "fullVideo", "status": "ready",
                                        "media_path": f"/m/{i}.mp4", "created_at": 100.0 + i})
    # Same session, other stream — must never be joined with the main one.
    for i in range(2):
        await event_store.upsert_media({"file_id": f"lowres{i}", "device_id": 1,
                                        "related_event": "r1",
                                        "category": "cloudDouble", "status": "ready",
                                        "media_path": f"/l/{i}.mp4", "created_at": 100.0 + i})
    # A lone chunk is not an episode.
    await event_store.upsert_media({"file_id": "solo", "device_id": 1, "related_event": "r2",
                                    "category": "fullVideo", "status": "ready",
                                    "media_path": "/m/solo.mp4", "created_at": 100.0})

    episodes = await event_store.stitch_candidates(("fullVideo", "cloudDouble"),
                                                   quiet_before_ts=200.0)
    keyed = {(e["related_event"], e["category"]): e for e in episodes}
    assert set(keyed) == {("r1", "fullVideo"), ("r1", "cloudDouble")}
    assert len(keyed[("r1", "fullVideo")]["chunks"]) == 3

    # Still being written to: nothing is a candidate yet.
    assert await event_store.stitch_candidates(("fullVideo",), quiet_before_ts=50.0) == []


async def test_stitch_candidates_skip_already_marked_chunks(event_store: EventStore):
    ids = [await event_store.upsert_media({"file_id": f"c{i}", "device_id": 1,
                                           "related_event": "r1",
                                           "category": "fullVideo", "status": "ready",
                                           "media_path": f"/m/{i}.mp4", "created_at": 100.0})
           for i in range(2)]
    await event_store.mark_stitch_failed(ids[:1], "failed")
    assert await event_store.stitch_candidates(("fullVideo",), quiet_before_ts=200.0) == []


async def test_replace_chunks_with_stitched_swaps_atomically(event_store: EventStore):
    ids = [await event_store.upsert_media({"file_id": f"c{i}", "device_id": 1,
                                           "related_event": "r1",
                                           "category": "fullVideo", "status": "ready",
                                           "media_path": f"/m/{i}.mp4"})
           for i in range(3)]
    merged = await event_store.replace_chunks_with_stitched(ids, {
        "file_id": "stitched:r1:fullVideo", "device_id": 1, "related_event": "r1",
        "category": "fullVideo", "media_path": "/m/joined.mp4", "status": "ready",
        "stitch_state": "stitched",
    })
    rows = await event_store.media_for_related_event("r1")
    assert [r["id"] for r in rows] == [merged]
    assert rows[0]["stitch_state"] == "stitched"


async def test_pets_crud_and_device_lookup(event_store: EventStore):
    pid = await event_store.upsert_pet({"name": "Mruczek", "device_ids_json": [1, 2]})
    pet = await event_store.get_pet(pid)
    assert pet["name"] == "Mruczek"
    assert (await event_store.pets_for_device(1))[0]["id"] == pid
    assert await event_store.pets_for_device(3) == []

    await event_store.upsert_pet({"name": "Mruczek Renamed"}, pet_id=pid)
    assert (await event_store.get_pet(pid))["name"] == "Mruczek Renamed"
    # untouched by the rename
    assert (await event_store.get_pet(pid))["device_ids_json"] == "[1, 2]"

    await event_store.delete_pet(pid)
    assert await event_store.get_pet(pid) is None
    assert await event_store.all_pets() == []


# The schema as it shipped before `events.parent_event` and `media.stitch_state`
# existed. A deployed add-on has a file in exactly this shape, and `create_all`
# will not touch a table that already exists — only `_migrate` upgrades it.
_OLD_SCHEMA = """
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_uid TEXT,
    related_event TEXT,
    device_id INTEGER NOT NULL,
    device_type TEXT,
    event_type TEXT,
    event_kind TEXT,
    ts REAL,
    source TEXT,
    pet_id INTEGER,
    score REAL,
    content_json TEXT,
    state_json TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE media (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id TEXT UNIQUE,
    device_id INTEGER NOT NULL,
    related_event TEXT,
    module_type TEXT,
    category TEXT,
    file_type TEXT,
    media_path TEXT,
    encrypted INTEGER DEFAULT 0,
    aes_iv TEXT,
    duration_ms INTEGER,
    start_ts REAL,
    end_ts REAL,
    pet_score REAL,
    pet_event TEXT,
    pet_id INTEGER,
    size_bytes INTEGER,
    status TEXT DEFAULT 'pending',
    created_at REAL NOT NULL
);
CREATE TABLE pets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    device_ids_json TEXT DEFAULT '[]',
    face_id TEXT,
    photo_path TEXT,
    weight REAL,
    created_at REAL NOT NULL
);
"""


async def test_migrate_adds_columns_to_an_existing_database_in_place():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "petkit.db"
        old = sqlite3.connect(str(path))
        old.executescript(_OLD_SCHEMA)
        old.execute("INSERT INTO events (event_uid, device_id, event_type, ts, created_at) "
                    "VALUES ('e1', 7, '10', 100.0, 100.0)")
        old.execute("INSERT INTO media (file_id, device_id, related_event, created_at) "
                    "VALUES ('f1', 7, 'r1', 100.0)")
        old.commit()
        old.close()

        store = EventStore(path)
        await store.connect()
        await store.connect()  # idempotent

        events = await store.all_events()
        assert len(events) == 1, "existing rows must survive the migration"
        assert events[0]["event_uid"] == "e1"
        assert events[0]["parent_event"] is None
        media = await store.get_media_by_file_id("f1")
        assert media["stitch_state"] is None

        # The new columns are real columns, not just dict keys.
        assert await store.update_event_fields(events[0]["id"], parent_event="p1") is True
        await store.mark_stitch_failed([media["id"]], "failed")
        assert (await store.get_event(events[0]["id"]))["parent_event"] == "p1"
        assert (await store.get_media_by_file_id("f1"))["stitch_state"] == "failed"
        await store.close()


async def test_opens_the_intended_file_when_the_path_needs_url_escaping():
    """A "?" in --data-dir must not truncate the database path.

    Interpolating the path into a URL string made everything downstream work
    against a NEW empty database created at the truncated path, while the real
    one sat untouched — a silent total history loss.
    """
    for directory in ("a?b", "a#b", "with space", "a+b%c"):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / directory / "petkit.db"
            path.parent.mkdir(parents=True)
            seed = sqlite3.connect(str(path))
            seed.executescript(_OLD_SCHEMA)
            seed.execute("INSERT INTO events (event_uid, device_id, event_type, ts, created_at) "
                         "VALUES ('kept', 7, '10', 100.0, 100.0)")
            seed.commit()
            seed.close()

            store = EventStore(path)
            await store.connect()
            events = await store.all_events()
            await store.upsert_event({"event_uid": "written", "device_id": 7, "ts": 200.0})
            await store.close()

            assert [e["event_uid"] for e in events] == ["kept"], directory
            written = sqlite3.connect(str(path))
            count = written.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            written.close()
            assert count == 2, f"{directory}: the write went somewhere else"
            assert [p.name for p in Path(tmp).iterdir()] == [directory], \
                f"{directory}: a stray database was created"


async def test_concurrent_writers_do_not_hit_database_is_locked(event_store: EventStore):
    """Writes from many coroutines at once — the pipeline, MQTT ingest, the
    sweeper and the stitcher all write on the same loop."""

    async def write_events(worker: int) -> None:
        for i in range(20):
            await event_store.upsert_event({"event_uid": f"w{worker}-{i}", "device_id": worker,
                                            "event_type": "10", "ts": float(i)})

    async def write_media(worker: int) -> None:
        for i in range(20):
            await event_store.upsert_media({"file_id": f"w{worker}-f{i}", "device_id": worker,
                                            "related_event": f"r{worker}", "status": "ready",
                                            "category": "fullVideo"})

    await asyncio.gather(*(write_events(w) for w in range(4)),
                         *(write_media(w) for w in range(4)),
                         event_store.query_timeline(), event_store.all_pets())

    assert len(await event_store.all_events()) == 80
    assert len(await event_store.media_for_retention("fullVideo")) == 80


# --- blocked_attempts (proxy mode's record of what the cloud tried) ---------

async def test_blocked_attempts_round_trip_newest_first(event_store: EventStore):
    written = await event_store.add_blocked_attempts([
        {"device_id": 1, "kind": "rce", "transport": "http",
         "endpoint": "/6/poll/t5/heartbeat", "payload_json": {"cmd": "rm -rf /"}},
        {"device_id": 1, "kind": "ota", "transport": "http",
         "endpoint": "/6/t5/dev_ota_check", "payload_json": "already a string"},
    ])
    assert written == 2

    rows = await event_store.recent_blocked_attempts()
    # Ordered by id descending, so several rows from one response keep a stable
    # order even though they share a created_at.
    assert [r["kind"] for r in rows] == ["ota", "rce"]
    # A non-str payload is JSON-encoded on the way in, a str is left alone.
    assert rows[0]["payload_json"] == "already a string"
    assert rows[1]["payload_json"] == '{"cmd": "rm -rf /"}'
    assert all(r["created_at"] > 0 for r in rows)


async def test_blocked_attempts_empty_batch_writes_nothing(event_store: EventStore):
    assert await event_store.add_blocked_attempts([]) == 0
    assert await event_store.recent_blocked_attempts() == []


async def test_blocked_attempts_filter_and_limit(event_store: EventStore):
    await event_store.add_blocked_attempts([
        {"device_id": 1, "kind": "rce"},
        {"device_id": 2, "kind": "ota"},
        {"device_id": 2, "kind": "rce"},
    ])
    assert len(await event_store.recent_blocked_attempts(device_id=2)) == 2
    assert len(await event_store.recent_blocked_attempts(kind="rce")) == 2
    assert len(await event_store.recent_blocked_attempts(device_id=2, kind="ota")) == 1
    assert len(await event_store.recent_blocked_attempts(limit=1)) == 1


async def test_blocked_attempts_drop_unknown_keys_and_allow_no_device(event_store: EventStore):
    """A proxied catch-all often has no device to attribute — that is exactly
    the request worth recording, so device_id is nullable here (unlike events)."""
    await event_store.add_blocked_attempts([
        {"kind": "rce", "endpoint": "/6/x", "some_future_field": "boom"},
    ])
    rows = await event_store.recent_blocked_attempts()
    assert rows[0]["device_id"] is None
    assert rows[0]["endpoint"] == "/6/x"


async def test_blocked_attempts_are_pruned_by_age(event_store: EventStore):
    await event_store.add_blocked_attempts([
        {"kind": "rce", "created_at": 10.0},
        {"kind": "ota", "created_at": 1000.0},
    ])
    assert await event_store.prune_blocked_attempts(before_ts=500.0) == 1
    assert [r["kind"] for r in await event_store.recent_blocked_attempts()] == ["ota"]


async def test_blocked_attempts_survive_a_reopen(event_store: EventStore):
    """Also proves the table is created on a database that predates it —
    `create_all` handles a missing table, so no _ADDED_COLUMNS entry is needed."""
    await event_store.add_blocked_attempts([{"kind": "secret", "endpoint": "/6/t5/dev_signup"}])
    await event_store.close()

    reopened = EventStore(event_store._path)
    rows = await reopened.recent_blocked_attempts()
    assert [r["kind"] for r in rows] == ["secret"]
    await reopened.close()


# --- the daily visit count that seeds Times Used ----------------------------

async def test_visit_count_counts_summaries_not_every_row_of_a_visit(
        event_store: EventStore):
    """One visit files several rows under the toilet kind: `pet_in` opens it and
    the `9` weight checks sample it, both sharing the kind with the `10`
    summary. Counting the kind reports three uses for one visit, which is how
    the seeded number and the running one would disagree by lunchtime."""
    now = time.time()
    for event_type in ("pet_in", "9", "10", "9", "10"):
        await event_store.upsert_event({"device_id": 7, "event_type": event_type,
                                        "event_kind": "toilet_visit", "ts": now})
    assert await event_store.device_visit_count_today(7, "t4") == 2


async def test_visit_count_is_cut_at_local_midnight(event_store: EventStore):
    now = time.time()
    await event_store.upsert_event({"device_id": 7, "event_type": "10",
                                    "event_kind": "toilet_visit",
                                    "ts": local_day_start(now) - 60})
    await event_store.upsert_event({"device_id": 7, "event_type": "10",
                                    "event_kind": "toilet_visit", "ts": now})
    assert await event_store.device_visit_count_today(7, "t4", now=now) == 1


async def test_visit_count_ignores_the_other_boxes(event_store: EventStore):
    now = time.time()
    await event_store.upsert_event({"device_id": 7, "event_type": "10",
                                    "event_kind": "toilet_visit", "ts": now})
    await event_store.upsert_event({"device_id": 8, "event_type": "10",
                                    "event_kind": "toilet_visit", "ts": now})
    assert await event_store.device_visit_count_today(7, "t4") == 1


async def test_visit_count_is_zero_for_a_device_that_has_no_visits(
        event_store: EventStore):
    """A feeder has no visit-summary code at all, so this must answer 0 rather
    than counting whatever its numeric codes happen to collide with."""
    now = time.time()
    await event_store.upsert_event({"device_id": 9, "event_type": "10",
                                    "event_kind": "feeding", "ts": now})
    assert await event_store.device_visit_count_today(9, "d4h") == 0
