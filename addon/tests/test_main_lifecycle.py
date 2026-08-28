"""Process lifecycle wiring in `petkit_local/main/`.

`main()` itself ends in `web.run_app`, so it cannot be called from a test.
What IS testable — and what actually broke in production — is the shutdown
contract around it: every background task is registered in one place, cancel
tolerates tasks that already died, and nothing that writes to the event store
is still running when it closes.
"""
import asyncio
import logging
import pathlib
import re

from aiohttp import web

from sqlalchemy.exc import SQLAlchemyError

from petkit_local.devices.base import Device
from petkit_local.devices.registry import DeviceRegistry
from petkit_local.events.normalize import visit_day_key
from petkit_local.http.proxy import close_proxy_session, get_proxy_session
from petkit_local.http.server import create_app
from petkit_local.main.lifecycle import (BACKGROUND_TASKS, _seed_visit_counts, _spawn,
                                         _stop_tasks)

CONFIG = {
    "api_url": "http://server/6/",
    "mqtt_port": 1883,
    "proxy_mode": False,
    "proxy_upstream": "",
    "proxy_block_run_cmd": True,
}

MAIN = pathlib.Path(__file__).resolve().parent.parent / "petkit_local" / "main"
MAIN_PY = MAIN / "__init__.py"
WIRING_PY = MAIN / "wiring.py"
LIFECYCLE_PY = MAIN / "lifecycle.py"


def _app() -> web.Application:
    """A bare app with the task list `_spawn` expects, as `main()` sets it up."""
    app = web.Application()
    app[BACKGROUND_TASKS] = []
    return app


async def _forever() -> None:
    await asyncio.sleep(3600)


# --- task registration -----------------------------------------------------


async def test_spawn_registers_every_task_for_shutdown():
    app = _app()
    a = _spawn(app, "one", _forever())
    b = _spawn(app, "two", _forever())

    assert app[BACKGROUND_TASKS] == [a, b]
    assert a.get_name() == "one" and b.get_name() == "two"

    await _stop_tasks(app[BACKGROUND_TASKS])


async def test_main_creates_no_task_outside_the_registration_helper():
    """The hardcoded task-name tuple drifted once; this is why it can't again.

    `_spawn` is the single place `main/lifecycle.py` may call `create_task`,
    because the shutdown path iterates what `_spawn` recorded. A second call
    site would be a task nothing cancels.
    """
    source = LIFECYCLE_PY.read_text()
    assert source.count("asyncio.create_task(") == 1
    # ...and it is the one inside _spawn.
    spawn_body = source.split("def _spawn(", 1)[1].split("\nasync def ", 1)[0]
    assert "asyncio.create_task(" in spawn_body


# --- cancel / await semantics ----------------------------------------------


async def test_stop_tasks_cancels_and_awaits_completion():
    app = _app()
    finished = []

    async def with_cleanup() -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            # A real task (MQTT bridge, stitcher) does teardown work here; if
            # shutdown only cancelled without awaiting, this would not run.
            finished.append("cleaned-up")
            raise

    _spawn(app, "with-cleanup", with_cleanup())
    await asyncio.sleep(0)  # let it reach the await

    await _stop_tasks(app[BACKGROUND_TASKS])

    assert finished == ["cleaned-up"]
    assert all(t.done() for t in app[BACKGROUND_TASKS])


async def test_stop_tasks_tolerates_an_already_finished_task():
    app = _app()

    async def quick() -> str:
        return "done"

    task = _spawn(app, "quick", quick())
    await task
    assert task.done()

    # Must not raise: cancel() on a done task is a no-op and awaiting it just
    # re-delivers the result.
    await _stop_tasks(app[BACKGROUND_TASKS])


async def test_stop_tasks_logs_a_task_that_died_rather_than_hiding_it():
    app = _app()

    async def boom() -> None:
        raise ValueError("publisher lost the broker")

    _spawn(app, "boom", boom())
    await asyncio.sleep(0)

    records = _capture()
    await _stop_tasks(app[BACKGROUND_TASKS])
    text = _release(records)

    assert "boom" in text
    assert "publisher lost the broker" in text
    assert "Traceback" in text  # log.exception, not a bare log.error


async def test_stop_tasks_tolerates_a_task_that_raises_on_cancel():
    """A task may fail *while* being cancelled — shutdown must still continue."""
    app = _app()
    reached = []

    async def bad_citizen() -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise RuntimeError("teardown blew up")

    async def polite() -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            reached.append("polite-cancelled")
            raise

    _spawn(app, "bad-citizen", bad_citizen())
    _spawn(app, "polite", polite())
    await asyncio.sleep(0)

    records = _capture()
    await _stop_tasks(app[BACKGROUND_TASKS])
    text = _release(records)

    # The failure is reported...
    assert "teardown blew up" in text
    # ...and the task queued behind it was still cancelled and awaited.
    assert reached == ["polite-cancelled"]


async def test_stop_tasks_does_not_swallow_a_plain_cancellation():
    """Only CancelledError from the task itself is swallowed — nothing else."""
    app = _app()
    _spawn(app, "a", _forever())
    _spawn(app, "b", _forever())
    await asyncio.sleep(0)

    records = _capture()
    await _stop_tasks(app[BACKGROUND_TASKS])
    text = _release(records)

    assert text == ""  # a normal cancellation is not an error


# --- shutdown ordering -----------------------------------------------------


async def test_media_pipeline_tasks_finish_before_the_store_would_close():
    """The invariant `event_store.close()` depends on, through the real signal.

    `dev_upload_file_info_v2` spawns pipeline tasks that outlive their request
    and write to the event store. `create_app` registers the drain on
    `on_cleanup`; `main()` appends `cleanup_background` (which closes the
    store) after it, and aiohttp fires cleanup callbacks in registration order.
    """
    from petkit_local.http.handlers import upload_file_info as ufi

    order = []

    async def media_work() -> None:
        await asyncio.sleep(0.05)
        order.append("media-task-finished")

    app = create_app(DeviceRegistry(), dict(CONFIG))

    async def closes_the_store(_app: web.Application) -> None:
        order.append("event-store-closed")

    # Registered exactly where main.py registers cleanup_background.
    app.on_cleanup.append(closes_the_store)

    runner = web.AppRunner(app)
    await runner.setup()

    # Stand in for handle_upload_file_info's own tracking (module-private set).
    task = asyncio.create_task(media_work())
    ufi._pending.add(task)
    task.add_done_callback(ufi._pending.discard)

    await runner.cleanup()

    assert order == ["media-task-finished", "event-store-closed"]
    assert ufi.pending_count() == 0


def test_cleanup_closes_the_event_store_last():
    """Source-order pin for the part of shutdown a unit test cannot reach.

    `cleanup_background` takes the whole `Services` bundle (the store, both
    registries, the sidecar), so the ordering is asserted on the source.
    Everything that can still write must appear before the `close()`.
    """
    source = LIFECYCLE_PY.read_text()
    body = source.split("async def cleanup_background(", 1)[1]
    # Drop the docstring: it names the same calls it is explaining.
    body = body.split('"""')[2]

    close = body.index("event_store.close()")
    for earlier in ("await wait_for_media_tasks()",
                    "await _stop_tasks(",
                    "await registry.stop()",
                    "await ble_registry.stop()"):
        assert body.index(earlier) < close, earlier


def test_registries_are_started_and_stopped():
    source = LIFECYCLE_PY.read_text()
    start = source.split("async def start_background(", 1)[1].split("async def cleanup_background(", 1)[0]
    assert "await registry.start()" in start
    assert "await ble_registry.start()" in start


def test_event_store_is_opened_before_anything_can_query_it():
    """The startup mirror of the shutdown-ordering pin above.

    The store is async now, so it cannot be opened in `main()` before
    `web.run_app`. It is opened at the top of `start_background`, which
    aiohttp runs to completion before the first request is served and before
    the MQTT bridge is spawned — so no reader can ever see an unmigrated DB.
    """
    source = LIFECYCLE_PY.read_text()
    start = source.split("async def start_background(", 1)[1].split("async def cleanup_background(", 1)[0]

    connect = start.index("await event_store.connect()")
    for later in ("await event_store.reclassify_media_categories(",
                  "await backfill_event_rows(event_store)",
                  '_spawn(app_instance, "mqtt-bridge"',
                  "create_panel_app("):
        assert connect < start.index(later), later

    # ...and nothing opens or migrates it back in the sync part of main(),
    # which is the composition root plus the entry point around it.
    before_hook = (source.split("async def start_background(", 1)[0]
                   + WIRING_PY.read_text() + MAIN_PY.read_text())
    assert "event_store.connect()" not in before_hook
    assert "reclassify_media_categories" not in before_hook
    assert "backfill_event_rows(" not in before_hook


def test_ssl_is_imported_once_and_the_logger_is_module_named():
    import petkit_local.main as main_module

    source = LIFECYCLE_PY.read_text()
    assert len(re.findall(r"^import ssl", source, re.M)) == 1
    assert "import ssl as _ssl" not in source
    assert 'logging.getLogger("petkit-local")' not in MAIN_PY.read_text()
    # Same name imported or run as `python3 -m petkit_local.main`.
    assert main_module.log.name == "petkit_local.main"


# --- proxy session ---------------------------------------------------------


async def test_proxy_session_is_closed_by_the_cleanup_hook():
    app = create_app(DeviceRegistry(), dict(CONFIG))
    app.on_cleanup.append(close_proxy_session)

    runner = web.AppRunner(app)
    await runner.setup()
    session = get_proxy_session(app)
    assert not session.closed

    await runner.cleanup()
    assert session.closed


async def test_proxy_cleanup_hook_is_harmless_without_a_session():
    app = create_app(DeviceRegistry(), dict(CONFIG))
    app.on_cleanup.append(close_proxy_session)

    runner = web.AppRunner(app)
    await runner.setup()
    await runner.cleanup()  # nothing ever proxied — must not raise


def test_main_registers_the_proxy_cleanup_hook():
    source = MAIN_PY.read_text()
    assert "app.on_cleanup.append(close_proxy_session)" in source
    # Must be registered before run_app freezes the app.
    assert source.index("app.on_cleanup.append(close_proxy_session)") < source.index("web.run_app(")


# --- log capture helper ----------------------------------------------------


class _Collector(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


def _capture() -> _Collector:
    handler = _Collector()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("petkit_local.main")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    return handler


def _release(handler: _Collector) -> str:
    logging.getLogger("petkit_local.main").removeHandler(handler)
    return "\n".join(handler.lines)


# --- seeding Times Used across a restart ------------------------------------

class _FakeStore:
    """Answers the visit count, and records who it was asked about."""

    def __init__(self, counts=None, raises=False):
        self.counts = counts or {}
        self.raises = raises
        self.asked: list[tuple[int, str]] = []

    async def device_visit_count_today(self, device_id, device_type, now=None):
        self.asked.append((device_id, device_type))
        if self.raises:
            raise SQLAlchemyError("db is gone")
        return self.counts.get(device_id, 0)


class _FakeRegistry:
    def __init__(self, devices):
        self._devices = devices

    def all(self):
        return self._devices


async def test_a_restart_restores_todays_times_used():
    """`Device.state` is not persisted, so without this a restart at 18:00
    reports a box used five times that day as unused until the next visit."""
    box = Device(petkit_id=100, device_type="t4", serial_number="SN")
    store = _FakeStore({100: 5})
    await _seed_visit_counts(_FakeRegistry([box]), store)
    assert box.state["usedTimes"] == 5
    assert box.state["usedTimesDay"] == visit_day_key()


async def test_a_box_with_its_own_counter_is_not_seeded():
    """Seeding a T5 would put our count where the device's own number belongs,
    and win until its next report."""
    box = Device(petkit_id=101, device_type="t5", serial_number="SN")
    store = _FakeStore({101: 5})
    await _seed_visit_counts(_FakeRegistry([box]), store)
    assert "usedTimes" not in box.state
    assert store.asked == []


async def test_a_store_that_cannot_answer_does_not_stop_startup():
    """The counter simply starts from the next visit — which is where it was
    before seeding existed. Failing startup over it would be worse."""
    box = Device(petkit_id=102, device_type="t4", serial_number="SN")
    await _seed_visit_counts(_FakeRegistry([box]), _FakeStore(raises=True))
    assert "usedTimes" not in box.state
