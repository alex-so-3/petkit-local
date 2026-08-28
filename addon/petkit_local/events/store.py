"""Persistent event/media/pet store — SQLite via SQLAlchemy's async engine.

The schema lives in `events/models.py`; this module is only the queries. Two
properties of it are load-bearing:

* **Every method returns plain dicts**, never ORM instances — call sites index
  rows with `row["key"]`/`row.get("key")` and `query_timeline` synthesises an
  extra `"media"` key onto each event row.
* **Callers pass loose dicts.** Keys that are not writable columns are dropped
  silently (`_accepted`), so a payload the device grew a field in can never
  turn into an exception on the storage path.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from sqlalchemy import URL, delete, event as sa_event, func, insert, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from petkit_local.events import codes
from petkit_local.events.models import Base, BlockedAttempt, Event, Media, Pet, PetFace
from petkit_local.utils.timeutil import local_day_start

log = logging.getLogger(__name__)

# How long a connection waits for a lock before giving up with "database is
# locked". Writes are already serialized in-process (see EventStore), so this
# only covers contention from outside it: a WAL checkpoint, a backup, or
# someone poking the file with the sqlite3 CLI.
BUSY_TIMEOUT_MS = 10_000

# These sessions are short-lived and hold no loaded entities, so there is
# nothing for a bulk UPDATE/DELETE to synchronize back into an identity map.
_NO_SYNC = {"synchronize_session": False}

# Columns added after the first release — SQLite has no "ADD COLUMN IF NOT
# EXISTS", so `_migrate` adds any that a pre-existing database is missing.
_ADDED_COLUMNS = {
    "events": {"parent_event": "TEXT", "pet_ref": "INTEGER"},
    "media": {"stitch_state": "TEXT"},
    "pets": {"device_pet_ids_json": "TEXT"},
}


def _writable(model: type[Base]) -> tuple[str, ...]:
    """Column names a caller's dict may set, in schema order.

    `id` belongs to SQLite and `created_at` is insert-only, so neither is
    writable through an upsert.
    """
    return tuple(c.name for c in model.__table__.columns
                 if c.name not in ("id", "created_at"))


_EVENT_COLUMNS = _writable(Event)
_MEDIA_COLUMNS = _writable(Media)
_PET_COLUMNS = _writable(Pet)
_BLOCKED_COLUMNS = _writable(BlockedAttempt)

#: What a device will accept before its own "Too many face picture" guard
#: trips. The firmware has the guard but its limit was not recoverable from the
#: strings; the official app's own upload grid is a 3x2 of six slots, so six is
#: what PetKit itself considers the maximum. A capture showing four faces is no
#: evidence against it: that is simply how many slots the household had filled.
MAX_FACES_PER_PET = 6


def _accepted(row: dict[str, Any], columns: tuple[str, ...]) -> dict[str, Any]:
    """The subset of `row` that names a writable column."""
    return {k: row[k] for k in columns if k in row}


def _created_at(row: dict[str, Any]) -> float:
    """Insert timestamp — the caller's if it supplied one, otherwise now.

    Honoured only on INSERT: `created_at` drives every age-based rule (media
    retention, stitch quiescence), so nothing may rewrite it afterwards.
    """
    given = row.get("created_at")
    return float(given) if isinstance(given, (int, float)) else time.time()


def _apply_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
    """Per-connection PRAGMAs, applied to every connection the pool opens.

    `journal_mode` is a property of the file and survives, but `busy_timeout`
    is per connection — a pooled connection without it fails instantly on the
    first lock it meets.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    cursor.close()


# The session of an open `transaction()`, scoped to the task that opened it.
# A plain attribute would be wrong: another task writing while a batch is open
# would join a session it does not own, and an AsyncSession cannot be used
# concurrently. Task-local, such a write simply waits on the write lock.
_batch: ContextVar[tuple[EventStore, AsyncSession] | None] = ContextVar(
    "petkit_event_store_batch", default=None)


class EventStore:
    """Async facade over the `events`, `media` and `pets` tables.

    Construction is synchronous and does no I/O; `connect()` creates the
    schema, migrates an older database in place and warms the pool. Methods
    connect on first use, so forgetting it degrades to a late migration rather
    than a crash — but main.py should still await it at startup, before
    anything queries.

    CONCURRENCY. SQLite permits exactly one writer, and an async engine hands
    out a pool of connections: the media pipeline, MQTT ingest, the retention
    sweeper, the stitcher and panel requests are all live at once on the same
    loop. Writes are therefore serialized behind a single `asyncio.Lock`.

    The trade-off is that writes get no parallelism from the pool — which
    costs nothing, since SQLite would serialize them anyway, and buys a hard
    guarantee that no coroutine ever sees SQLITE_BUSY caused by this process.
    Reads stay concurrent: under WAL a reader never blocks a writer, so the
    panel's timeline query does not contend with the pipeline's inserts.
    """

    def __init__(self, db_path: str | Path) -> None:
        """Prepare the engine for `db_path`, creating its parent directory.

        No connection is opened and no schema is created here - `connect()`
        does that, and every query awaits `_ensure_connected()` first, so an
        `EventStore` can be constructed at import/wiring time.
        """
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Built from components, never interpolated into a URL string: a "?" in
        # the path would start a query string and silently truncate it, opening
        # (and creating) a different, empty database while the real one sits
        # untouched — the worst possible failure, since everything then "works".
        url = URL.create("sqlite+aiosqlite", database=str(self._path))
        self._engine = create_async_engine(url)
        sa_event.listen(self._engine.sync_engine, "connect", _apply_pragmas)
        self._sessions = async_sessionmaker(self._engine, expire_on_commit=False)
        self._write_lock = asyncio.Lock()
        self._ready = False

    async def connect(self) -> None:
        """Create the schema and migrate an existing database in place.

        Idempotent: safe to await from several startup paths, and again after
        `close()`.
        """
        async with self._write_lock:
            if self._ready:
                return
            async with self._engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                await self._migrate(conn)
            self._ready = True
        # Outside the lock on purpose: this runs ordinary writes, which take
        # the same lock again. `_ready` is already set, so a failure here must
        # not leave the store looking connected but half-migrated — it is
        # logged and retried on the next `connect()` rather than swallowed.
        try:
            await self._migrate_legacy_faces()
        except SQLAlchemyError:
            self._ready = False
            raise

    async def close(self) -> None:
        """Dispose the connection pool. Idempotent."""
        self._ready = False
        await self._engine.dispose()

    async def _migrate(self, conn: AsyncConnection) -> None:
        """Add columns introduced after a database was first created.

        `create_all` only creates missing TABLES, so an add-on that has been
        running since an earlier version keeps its old column set and every
        query naming a new column fails against it.
        """
        for table, columns in _ADDED_COLUMNS.items():
            info = await conn.exec_driver_sql(f"PRAGMA table_info({table})")
            existing = {row[1] for row in info.fetchall()}
            for name, coltype in columns.items():
                if name not in existing:
                    await conn.exec_driver_sql(
                        f"ALTER TABLE {table} ADD COLUMN {name} {coltype}")
                    log.info("Migrated %s: added column %s", table, name)

    async def _migrate_legacy_faces(self) -> None:
        """Carry a pre-`pet_faces` single photo into the new table.

        `pets.photo_path` used to hold the one reference photo per pet. Rather
        than leave those installs with a pet the device is told nothing about,
        the file is adopted as that pet's first `PetFace`. Only pets with no
        face rows at all are touched, so this is idempotent and cannot
        duplicate a photo the user has since re-uploaded.

        A path whose FILE IS GONE is skipped. `ai/pets.py::add_face` takes care
        never to leave a row pointing at a missing file — the device would be
        handed a URL that 404s and would retry it forever — and a migration
        must not create the very row the write path refuses to.
        """
        async with self._write() as session:
            legacy = await session.scalars(select(Pet).where(Pet.photo_path.isnot(None)))
            for pet in list(legacy):
                already = await session.scalar(
                    select(func.count()).select_from(PetFace).where(PetFace.pet_id == pet.id))
                if already:
                    continue
                if not os.path.isfile(pet.photo_path):
                    log.warning("Pet %s's legacy face photo is missing (%s); not migrating it",
                                pet.id, pet.photo_path)
                    continue
                session.add(PetFace(pet_id=pet.id, photo_path=pet.photo_path,
                                    created_at=pet.created_at))
                log.info("Migrated pet %s's legacy face photo into pet_faces", pet.id)

    async def _ensure_connected(self) -> None:
        """Create/migrate the schema on first use, so callers never have to."""
        if not self._ready:
            await self.connect()

    @asynccontextmanager
    async def _read(self) -> AsyncIterator[AsyncSession]:
        """A session for statements that only read.

        Deliberately takes no write lock: under WAL a reader never blocks and
        is never blocked, so the panel's timeline query does not contend with
        the media pipeline's inserts.
        """
        await self._ensure_connected()
        async with self._sessions() as session:
            yield session

    @asynccontextmanager
    async def _write(self) -> AsyncIterator[AsyncSession]:
        """A session for statements that write.

        Joins the calling task's open `transaction()` when there is one — the
        batch owns the commit — and otherwise takes the write lock for the
        duration of a single committed statement group.
        """
        batch = _batch.get()
        if batch is not None and batch[0] is self:
            yield batch[1]
            return
        await self._ensure_connected()
        async with self._write_lock:
            async with self._sessions() as session:
                yield session
                await session.commit()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[EventStore]:
        """Group many writes into one commit.

        Every write method commits on its own, which is right for the one-row
        writes the device drives but wrong for a pass that rewrites thousands
        of rows: that pays an fsync each time and leaves the table half-updated
        if it dies midway. Used by `events/migrations.py::backfill_event_rows`.
        """
        await self._ensure_connected()
        async with self._write_lock:
            async with self._sessions() as session:
                token = _batch.set((self, session))
                try:
                    yield self
                    await session.commit()
                finally:
                    _batch.reset(token)

    # --- events -------------------------------------------------------

    async def upsert_event(self, row: dict[str, Any]) -> int:
        """Insert an event, or update it in place if `event_uid` already
        exists (a device retrying the same dev_event_report/MQTT event)."""
        data = _accepted(row, _EVENT_COLUMNS)
        for json_key in ("content_json", "state_json"):
            if json_key in data and not isinstance(data[json_key], str):
                data[json_key] = json.dumps(data[json_key])

        async with self._write() as session:
            existing = None
            if data.get("event_uid"):
                existing = await session.scalar(
                    select(Event).where(Event.event_uid == data["event_uid"]))
            if existing is not None:
                for key, value in data.items():
                    setattr(existing, key, value)
                await session.flush()
                return existing.id

            obj = Event(created_at=_created_at(row), **data)
            session.add(obj)
            await session.flush()
            return obj.id

    async def get_event(self, event_id: int) -> dict[str, Any] | None:
        """One event row by primary key, or None if it no longer exists."""
        async with self._read() as session:
            obj = await session.get(Event, event_id)
            return obj.as_dict() if obj else None

    async def all_events(self, limit: int = 100000) -> list[dict[str, Any]]:
        """Every event row in insert order, oldest first.

        Unlike `query_timeline` this attaches no media and applies no window;
        it exists for export/diagnostics, not for the panel.
        """
        async with self._read() as session:
            rows = await session.scalars(select(Event).order_by(Event.id).limit(limit))
            return [r.as_dict() for r in rows]

    async def update_event_fields(self, event_id: int, **fields: Any) -> bool:
        """Overwrite named columns on one event.

        Returns:
            False if the call named no writable column, True otherwise —
            including when no row has that id.
        """
        data = {k: v for k, v in fields.items() if k in _EVENT_COLUMNS}
        if not data:
            return False
        async with self._write() as session:
            await session.execute(
                update(Event).where(Event.id == event_id).values(**data),
                execution_options=_NO_SYNC)
        return True

    async def event_by_related(self, related_event: str) -> dict[str, Any] | None:
        """Most recent event sharing this related_event (session) id.

        Used to recover an `event_kind` label for media that arrives without
        its own content flags (see media/pipeline.py).
        """
        async with self._read() as session:
            obj = await session.scalar(
                select(Event)
                .where(Event.related_event == related_event)
                .order_by(Event.ts.desc())
                .limit(1))
            return obj.as_dict() if obj else None

    async def prune_events(self, before_ts: float) -> int:
        """Delete events older than `before_ts`; returns how many went.

        Rows with no timestamp are kept: a NULL `ts` means the device never
        told us when it happened, not that it is old.
        """
        async with self._write() as session:
            result = await session.execute(
                delete(Event).where(Event.ts.is_not(None), Event.ts < before_ts),
                execution_options=_NO_SYNC)
            return result.rowcount

    # --- blocked attempts -----------------------------------------------

    async def add_blocked_attempts(self, rows: list[dict[str, Any]]) -> int:
        """Record what the upstream cloud tried and did not get to do.

        Batched into one transaction because a single proxied response can trip
        several rules at once, and this runs on the device-facing request path.

        Insert-only: unlike `upsert_event` there is no dedup key, because two
        identical attempts a minute apart are two attempts and the second one is
        the interesting one.

        Uses a Core `executemany` rather than `session.add()` in a loop, which
        is also what avoids SQLAlchemy's "insertmanyvalues" sentinel rule: the
        ORM path wants to read each new `id` back, and refuses to batch that
        against a primary key declared `nullable=True` (the house convention —
        see `events/models.py`). Nothing here needs the ids. Every row is padded
        to the full column set first, because an executemany requires one shape.

        Returns:
            How many rows were written. Zero for an empty list, without opening
            a session — the overwhelmingly common case, since nothing is
            recorded while proxy mode is off.
        """
        if not rows:
            return 0

        payload = []
        for row in rows:
            data = _accepted(row, _BLOCKED_COLUMNS)
            for json_key in ("payload_json", "detail_json"):
                if json_key in data and not isinstance(data[json_key], str):
                    data[json_key] = json.dumps(data[json_key], default=str)
            payload.append({col: data.get(col) for col in _BLOCKED_COLUMNS}
                           | {"created_at": _created_at(row)})

        async with self._write() as session:
            await session.execute(insert(BlockedAttempt), payload)
        return len(payload)

    async def recent_blocked_attempts(self, limit: int = 200, device_id: int | None = None,
                                      kind: str | None = None) -> list[dict[str, Any]]:
        """The most recent attempts, newest first.

        Ordered by `id` rather than `created_at`: several rows from one response
        share a timestamp to the microsecond, and the panel needs a stable order.
        """
        async with self._read() as session:
            query = select(BlockedAttempt)
            if device_id is not None:
                query = query.where(BlockedAttempt.device_id == device_id)
            if kind:
                query = query.where(BlockedAttempt.kind == kind)
            rows = await session.scalars(
                query.order_by(BlockedAttempt.id.desc()).limit(limit))
            return [r.as_dict() for r in rows]

    async def prune_blocked_attempts(self, before_ts: float) -> int:
        """Delete attempts recorded before `before_ts`; returns how many went.

        Keyed on `created_at` and not on a device-supplied timestamp, because
        there is none: when we noticed is the only time these rows have.
        """
        async with self._write() as session:
            result = await session.execute(
                delete(BlockedAttempt).where(BlockedAttempt.created_at < before_ts),
                execution_options=_NO_SYNC)
            return result.rowcount

    # --- media ----------------------------------------------------------

    async def upsert_media(self, row: dict[str, Any]) -> int:
        """Insert a media row keyed by `file_id`, or update it in place.

        `file_id` is unique because the pipeline calls this twice for the same
        file: once when `dev_upload_file_info_v2` arrives (metadata only) and
        again once the bytes are decrypted/remuxed (media_path/status/size).

        Raises:
            ValueError: if the row carries no `file_id` to key on.
        """
        data = _accepted(row, _MEDIA_COLUMNS)
        file_id = data.get("file_id")
        if not file_id:
            raise ValueError("media row requires file_id")

        async with self._write() as session:
            existing = await session.scalar(select(Media).where(Media.file_id == file_id))
            if existing is not None:
                for key, value in data.items():
                    if key != "file_id":
                        setattr(existing, key, value)
                await session.flush()
                return existing.id

            obj = Media(created_at=_created_at(row), **data)
            session.add(obj)
            await session.flush()
            return obj.id

    async def get_media_by_file_id(self, file_id: str) -> dict[str, Any] | None:
        """One media row by the device's own file id, or None."""
        async with self._read() as session:
            obj = await session.scalar(select(Media).where(Media.file_id == file_id))
            return obj.as_dict() if obj else None

    async def media_for_related_event(self, related_event: str) -> list[dict[str, Any]]:
        """Every media row of one session, in arrival order.

        Arrival order is what makes a burst (waste/health gallery) render in
        the order the device shot it, and what lets `media/stitch.py` see
        video chunks in sequence.
        """
        async with self._read() as session:
            rows = await session.scalars(
                select(Media)
                .where(Media.related_event == related_event)
                .order_by(Media.created_at))
            return [r.as_dict() for r in rows]

    async def latest_media(self, device_id: int,
                           category: str | None = None) -> dict[str, Any] | None:
        """Newest playable media for one device, optionally of one role.

        Only `status == "ready"` rows qualify, so this never hands back a file
        that is still encrypted or mid-remux. `category` is a media ROLE
        (`fullVideo`, `eventImage`, ...), not an STS capability.
        """
        stmt = (select(Media)
                .where(Media.device_id == device_id, Media.status == "ready")
                .order_by(Media.created_at.desc())
                .limit(1))
        if category:
            stmt = stmt.where(Media.category == category)
        async with self._read() as session:
            obj = await session.scalar(stmt)
            return obj.as_dict() if obj else None

    async def media_for_retention(self, category: str) -> list[dict[str, Any]]:
        """All ready media of one category, newest first.

        Newest-first is what the retention sweeper wants: it accumulates the
        rows it keeps from the front and deletes the tail, so the oldest file
        is always the first casualty of a size or age cap.
        """
        async with self._read() as session:
            rows = await session.scalars(
                select(Media)
                .where(Media.category == category, Media.status == "ready")
                .order_by(Media.created_at.desc()))
            return [r.as_dict() for r in rows]

    async def delete_media(self, media_id: int) -> None:
        """Forget one media row. The file on disk is the caller's problem."""
        async with self._write() as session:
            await session.execute(delete(Media).where(Media.id == media_id),
                                  execution_options=_NO_SYNC)

    async def reclassify_media_categories(self, module_type_to_category: dict[str, str]) -> int:
        """Recompute `category` from `module_type`; returns the rows fixed.

        Rows written before a mapping was corrected would otherwise keep a
        wrong category forever - which for video means two different streams
        sharing one category, and media/stitch.py refusing to join (or worse,
        joining) mismatched chunks. The caller passes the mapping in so this
        stays a pure storage operation with no protocol knowledge.
        """
        fixed = 0
        async with self._write() as session:
            for module_type, category in (module_type_to_category or {}).items():
                result = await session.execute(
                    update(Media)
                    .where(Media.module_type == module_type, Media.category != category)
                    .values(category=category),
                    execution_options=_NO_SYNC)
                fixed += result.rowcount
        if fixed:
            log.info("Reclassified %d media rows from their module_type", fixed)
        return fixed

    # --- stitching (see media/stitch.py) ----------------------------------

    async def stitch_candidates(self, categories: tuple[str, ...], quiet_before_ts: float,
                                min_chunks: int = 2) -> list[dict[str, Any]]:
        """Episodes whose chunks look complete and are worth concatenating.

        An episode qualifies when it has >= `min_chunks` ready chunks of ONE
        category under one related_event, nothing was added to it since
        `quiet_before_ts`, and no earlier attempt marked it (`stitch_state`)
        - otherwise a file ffmpeg cannot handle would be retried forever.

        Returns:
            `[{device_id, related_event, category, chunks: [media row, ...]}]`,
            each chunk list in device timestamp order rather than insert
            order, because a chunk can be re-uploaded out of sequence.
        """
        if not categories:
            return []

        unclaimed = (Media.status == "ready", Media.stitch_state.is_(None))
        chunk_count = func.count().label("n")
        newest = func.max(Media.created_at).label("newest")
        episodes = (
            select(Media.device_id, Media.related_event, Media.category, chunk_count, newest)
            .where(*unclaimed,
                   Media.related_event.is_not(None),
                   Media.media_path.is_not(None),
                   Media.category.in_(categories))
            .group_by(Media.device_id, Media.related_event, Media.category)
            .having(chunk_count >= min_chunks)
            .having(newest < quiet_before_ts))

        out: list[dict[str, Any]] = []
        async with self._read() as session:
            groups = (await session.execute(episodes)).mappings().all()
            for group in groups:
                rows = await session.scalars(
                    select(Media)
                    .where(*unclaimed,
                           Media.device_id == group["device_id"],
                           Media.related_event == group["related_event"],
                           Media.category == group["category"])
                    # Device order, not insert order: a chunk can be re-uploaded.
                    .order_by(func.coalesce(Media.start_ts, Media.created_at),
                              Media.created_at))
                chunks = [r.as_dict() for r in rows]
                if len(chunks) >= min_chunks:
                    out.append({
                        "device_id": group["device_id"],
                        "related_event": group["related_event"],
                        "category": group["category"],
                        "chunks": chunks,
                    })
        return out

    async def mark_stitch_failed(self, media_ids: list[int], reason: str = "failed") -> None:
        """Tag chunks so `stitch_candidates` stops offering them.

        The chunks themselves are deliberately kept - a failed join must never
        cost the user footage - and `reason` is stored verbatim in
        `stitch_state`, which is also how a successful join marks its output.
        """
        if not media_ids:
            return
        async with self._write() as session:
            await session.execute(
                update(Media).where(Media.id.in_(media_ids)).values(stitch_state=reason),
                execution_options=_NO_SYNC)

    async def replace_chunks_with_stitched(self, chunk_ids: list[int],
                                           merged_row: dict[str, Any]) -> int:
        """Swap a set of chunk rows for the single stitched row atomically.

        One transaction, so a crash can never leave the timeline showing
        neither the chunks (already deleted) nor the merged clip (not yet
        inserted).

        Returns:
            The id of the new merged row.

        Raises:
            SQLAlchemyError: re-raised after logging, because the caller must
                not delete the chunk FILES if their rows survived.
        """
        data = _accepted(merged_row, _MEDIA_COLUMNS)
        try:
            async with self._write() as session:
                if chunk_ids:
                    await session.execute(delete(Media).where(Media.id.in_(chunk_ids)),
                                          execution_options=_NO_SYNC)
                obj = Media(created_at=_created_at(merged_row), **data)
                session.add(obj)
                await session.flush()
                return obj.id
        except SQLAlchemyError:
            log.exception("Failed to swap %d chunks for stitched row", len(chunk_ids))
            raise

    # --- timeline (sessions) ---------------------------------------------

    async def query_timeline(self, device_id: int | None = None, start_ts: float | None = None,
                             end_ts: float | None = None, event_kind: str | None = None,
                             limit: int = 200) -> list[dict[str, Any]]:
        """Raw event rows for a window, newest first, each carrying its media.

        Every row gains a synthetic `"media"` key holding the media rows that
        share its `related_event` (always a list, possibly empty). Session
        grouping - visit plus its sub-events - is NOT done here but by
        `events/sessions.py::group_sessions` on top of this result, keeping the
        grouping heuristic out of SQL where it stays readable and testable.
        """
        stmt = select(Event)
        if device_id is not None:
            stmt = stmt.where(Event.device_id == device_id)
        if start_ts is not None:
            stmt = stmt.where(Event.ts >= start_ts)
        if end_ts is not None:
            stmt = stmt.where(Event.ts < end_ts)
        if event_kind is not None:
            stmt = stmt.where(Event.event_kind == event_kind)
        stmt = stmt.order_by(Event.ts.desc()).limit(limit)

        media_by_related: dict[str, list[dict[str, Any]]] = {}
        async with self._read() as session:
            events = [e.as_dict() for e in await session.scalars(stmt)]
            # One extra query for the whole page rather than one per event.
            related = sorted({e["related_event"] for e in events if e.get("related_event")})
            if related:
                rows = await session.scalars(
                    select(Media)
                    .where(Media.related_event.in_(related))
                    .order_by(Media.created_at))
                for media in rows:
                    media_by_related.setdefault(media.related_event, []).append(media.as_dict())

        for e in events:
            e["media"] = media_by_related.get(e.get("related_event") or "", [])
        return events

    # --- pets -------------------------------------------------------------

    async def upsert_pet(self, row: dict[str, Any], pet_id: int | None = None) -> int:
        """Create a pet, or update the one named by `pet_id`; returns its id.

        `device_ids_json` may be passed as a real list and is serialised here,
        but it always reads BACK as the raw JSON string (see `get_pet`).

        The returned id is what the device echoes as `petId` in recognition
        events: we hand our own primary key to the device via `dev_discern_pic`,
        so no id-mapping protocol exists or is needed.
        """
        data = _accepted(row, _PET_COLUMNS)
        if "device_ids_json" in data and not isinstance(data["device_ids_json"], str):
            data["device_ids_json"] = json.dumps(data["device_ids_json"])

        async with self._write() as session:
            if pet_id is not None:
                if data:
                    await session.execute(
                        update(Pet).where(Pet.id == pet_id).values(**data),
                        execution_options=_NO_SYNC)
                return pet_id

            obj = Pet(created_at=_created_at(row), **data)
            session.add(obj)
            await session.flush()
            return obj.id

    async def get_pet(self, pet_id: int) -> dict[str, Any] | None:
        """One pet row by id, or None.

        `device_ids_json` comes back as the stored JSON STRING, not a list -
        the asymmetry with `upsert_pet` is a trap worth remembering.
        """
        async with self._read() as session:
            obj = await session.get(Pet, pet_id)
            return obj.as_dict() if obj else None

    async def all_pets(self) -> list[dict[str, Any]]:
        """Every pet row, oldest first (same row shape as `get_pet`)."""
        async with self._read() as session:
            rows = await session.scalars(select(Pet).order_by(Pet.id))
            return [r.as_dict() for r in rows]

    async def delete_pet(self, pet_id: int) -> None:
        """Forget one pet and its face rows. Events keep its id."""
        async with self._write() as session:
            await session.execute(delete(PetFace).where(PetFace.pet_id == pet_id),
                                  execution_options=_NO_SYNC)
            await session.execute(delete(Pet).where(Pet.id == pet_id),
                                  execution_options=_NO_SYNC)

    # --- pet faces ----------------------------------------------------------

    async def pet_faces(self, pet_id: int) -> list[dict[str, Any]]:
        """One pet's reference photos, oldest first."""
        async with self._read() as session:
            rows = await session.scalars(
                select(PetFace).where(PetFace.pet_id == pet_id).order_by(PetFace.id))
            return [r.as_dict() for r in rows]

    async def add_pet_face(self, pet_id: int, photo_path: str) -> int | None:
        """Register a photo and return its new id, or None if the pet is full.

        The id is what the device is handed as `discern[].id`, so it is
        allocated here rather than by the caller — see `PetFace`.
        """
        async with self._write() as session:
            count = await session.scalar(
                select(func.count()).select_from(PetFace).where(PetFace.pet_id == pet_id))
            if (count or 0) >= MAX_FACES_PER_PET:
                return None
            obj = PetFace(pet_id=pet_id, photo_path=photo_path, created_at=time.time())
            session.add(obj)
            await session.flush()
            return obj.id

    async def update_pet_face_path(self, face_id: int, photo_path: str) -> None:
        """Point a face row at its file, once the id that names it is known."""
        async with self._write() as session:
            await session.execute(
                update(PetFace).where(PetFace.id == face_id).values(photo_path=photo_path),
                execution_options=_NO_SYNC)

    async def get_pet_face(self, face_id: int) -> dict[str, Any] | None:
        """One face row by id, or None."""
        async with self._read() as session:
            obj = await session.get(PetFace, face_id)
            return obj.as_dict() if obj else None

    async def delete_pet_face(self, face_id: int) -> None:
        """Forget one reference photo. The file is the caller's to unlink."""
        async with self._write() as session:
            await session.execute(delete(PetFace).where(PetFace.id == face_id),
                                  execution_options=_NO_SYNC)

    # --- reported identities ------------------------------------------------

    async def unbound_pet_refs(self) -> list[dict[str, Any]]:
        """Identities the device reported that map to no pet row.

        `[{pet_ref, count, last_ts}]`, busiest first. These are almost always
        ids the box cached from PetKit's cloud before the takeover; the panel
        offers them for binding rather than guessing at one.

        "Known" means an alias somebody already bound. Our OWN `pets.id` is
        deliberately NOT excluded: a row can carry a ref that is one of our
        pets and still have a NULL `pet_id` — it was ingested while the pet
        registry was unavailable, or recovered by `backfill_event_rows`, which
        recomputes `pet_ref` from stored content but cannot resolve it. Hiding
        those left them unattributed forever with nothing offering to fix it.
        Rows that already resolved are excluded instead, so a healthy install
        still shows an empty list.
        """
        bound: set[int] = set()
        for pet in await self.all_pets():
            try:
                bound.update(int(x) for x in json.loads(pet.get("device_pet_ids_json") or "[]"))
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        async with self._read() as session:
            rows = await session.execute(
                select(Event.pet_ref, func.count(), func.max(Event.ts))
                .where(Event.pet_ref.isnot(None), Event.pet_id.is_(None))
                .group_by(Event.pet_ref))
            return sorted(
                ({"pet_ref": ref, "count": n, "last_ts": last}
                 for ref, n, last in rows if ref not in bound),
                key=lambda r: r["count"], reverse=True)

    async def bind_pet_ref(self, pet_ref: int, pet_id: int | None) -> int:
        """Re-attribute every past event carrying `pet_ref` to `pet_id`.

        Returns how many rows changed. Run when the user binds a foreign
        identity, so history stops being anonymous the moment they do — and
        with `pet_id=None` when they UNBIND one, which has to clear the same
        rows or history would stay attributed to a pet that no longer claims
        the id while new events resolve to nobody.
        """
        async with self._write() as session:
            result = await session.execute(
                update(Event).where(Event.pet_ref == pet_ref).values(pet_id=pet_id),
                execution_options=_NO_SYNC)
            return int(result.rowcount or 0)

    async def pets_claiming_ref(self, pet_ref: int) -> list[int]:
        """Ids of every pet whose alias list contains `pet_ref`.

        Normally zero or one. More than one is a conflict the caller has to
        resolve rather than silently pick from — `resolve_pet_ref` would take
        the lowest id, so past and future events could end up on different
        animals with nothing saying so.
        """
        out: list[int] = []
        for pet in await self.all_pets():
            try:
                aliases = json.loads(pet.get("device_pet_ids_json") or "[]")
            except (json.JSONDecodeError, TypeError):
                continue
            if pet_ref in aliases:
                out.append(pet["id"])
        return out

    async def pet_visit_stats(self, pet_id: int, now: float | None = None) -> dict[str, Any]:
        """Aggregate stats backing one pet's virtual HA device.

        Returns `{last_visit_ts, visits_today, last_visit_weight,
        last_visit_duration, last_device_id}`, every value None/0 for a pet
        that has never been recognised - the entities exist from the moment
        the pet does (see ha/publisher.py::publish_pet_state).

        `visits_today` counts from LOCAL midnight, which is the boundary the
        owner means by "today". Counting from UTC midnight instead credits a
        00:30 visit on a UTC+2 install to the previous day, and the sensor
        reads low every morning until the offset has passed.
        """
        now = now if now is not None else time.time()
        day_start = local_day_start(now)
        visit = (Event.pet_id == pet_id, Event.event_kind == codes.KIND_TOILET)

        async with self._read() as session:
            latest_row = await session.scalar(
                select(Event).where(*visit).order_by(Event.ts.desc()).limit(1))
            latest = latest_row.as_dict() if latest_row else None
            visits_today = await session.scalar(
                select(func.count()).select_from(Event).where(*visit, Event.ts >= day_start))

            weight = None
            duration = None
            device_id = None
            last_visit_ts = None
            if latest:
                device_id = latest.get("device_id")
                last_visit_ts = latest.get("ts")
                content = {}
                if latest.get("content_json"):
                    try:
                        content = json.loads(latest["content_json"])
                    except (json.JSONDecodeError, TypeError):
                        content = {}
                w = content.get("pet_weight", content.get("petWeight"))
                try:
                    # The scale reports WHOLE GRAMS -- all 58 `pet_weight`
                    # values across the captures are integers. Going through
                    # float() only to publish "2223.0 g" invents a decimal the
                    # hardware never measured. Parsed as float first so a
                    # numeric string still works.
                    weight = round(float(w)) if w is not None else None
                except (TypeError, ValueError):
                    weight = None
                if latest.get("related_event"):
                    pet_in_ts = await session.scalar(
                        select(Event.ts)
                        .where(Event.related_event == latest["related_event"],
                               Event.event_type == "pet_in")
                        .order_by(Event.ts.asc())
                        .limit(1))
                    if pet_in_ts is not None and last_visit_ts is not None:
                        # WHOLE SECONDS. Both stamps are report arrival times,
                        # so the sub-second part is transport latency, not how
                        # long the cat was in the box -- `codes.py` measures a
                        # 3.8s median for `ts - start_time` on exactly this
                        # kind of pair. Publishing the raw float subtraction
                        # gave HA "57.317591428756714 s", which is fifteen
                        # digits of precision the inputs cannot support.
                        duration = round(max(0.0, last_visit_ts - pet_in_ts))

        return {
            "last_visit_ts": last_visit_ts,
            "visits_today": visits_today,
            "last_visit_weight": weight,
            "last_visit_duration": duration,
            "last_device_id": device_id,
        }

    async def device_visit_count_today(self, device_id: int, device_type: str,
                                       now: float | None = None) -> int:
        """How many completed toilet visits this device recorded since local midnight.

        Backs the Times Used sensor on the models that never report their own
        counter (`utils/const.py::DEVICE_TYPES_LITTER_USED_TIMES_UNREPORTED`).
        `main/lifecycle.py` calls it once at startup to seed the running count
        that `events/normalize.py::_accumulate_visit_count` then keeps.

        Counts the VISIT-SUMMARY event types, not `event_kind == toilet_visit`:
        one visit stores several rows under that kind — the `pet_in` start and
        the mid-visit `9` weight checks share it with the `10` summary — so
        counting the kind reports two or three uses for every one.

        Local midnight, for the reason spelled out in `pet_visit_stats`.
        """
        types = codes.visit_summary_event_types(device_type)
        if not types:
            return 0
        day_start = local_day_start(now if now is not None else time.time())
        async with self._read() as session:
            return await session.scalar(
                select(func.count()).select_from(Event).where(
                    Event.device_id == device_id,
                    Event.event_type.in_(types),
                    Event.ts >= day_start)) or 0

    async def pets_for_device(self, device_id: int) -> list[dict[str, Any]]:
        """Pets linked to one device, filtered in Python rather than in SQL.

        `device_ids_json` is an opaque JSON string column, so there is nothing
        to index or join on; the table holds a handful of rows per household,
        which is why a full scan is the right trade rather than a schema
        change. A row whose JSON is unparseable is treated as linked to
        nothing instead of failing the whole listing.
        """
        out: list[dict[str, Any]] = []
        for p in await self.all_pets():
            try:
                ids = json.loads(p.get("device_ids_json") or "[]")
            except (json.JSONDecodeError, TypeError):
                ids = []
            if device_id in ids:
                out.append(p)
        return out
