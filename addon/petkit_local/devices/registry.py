"""Device registry: in-memory device state plus crash-safe JSON persistence.

Also home to `PersistedRegistry`, the debounced writer shared with
`devices/ble/` — see its docstring for why both registries need it.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from petkit_local.devices import defaults
from petkit_local.devices.base import Device
from petkit_local.utils.jsonio import atomic_write_json, atomic_write_text, read_json

log = logging.getLogger(__name__)

# Distinguishes "read_json fell back" from "the file legitimately contained
# null", which no default value can do on its own.
_UNREADABLE = object()


def _merge_default_settings(device: Device) -> bool:
    """Give a device every default settings key it is missing, in place.

    `setdefault("settings", ...)` only fires when the whole block is absent, so
    a device registered by an older build never gained a key added later — the
    reference T5 was missing ~25 of them, including `petDetection`,
    `toiletDetection` and `sandFullWeight`, which meant `dev_device_info` told
    it less about itself than the real cloud does.

    Existing values are never overwritten: whatever the device or Home
    Assistant last set stays authoritative, and only the gaps are filled. The
    one exception is the old W7H seed (`defaults.retire_stale_seed`).

    Returns:
        True when that cleanup removed something, so the caller can persist it.
    """
    removed = defaults.retire_stale_seed(device)
    if removed:
        log.info("Device %d: dropped the old %s seed %s, which it never reported",
                 device.petkit_id, device.device_type, removed)
    settings = device.config.setdefault("settings", {})
    if isinstance(settings, dict):
        for key, value in defaults.default_settings(device).items():
            settings.setdefault(key, value)
    return bool(removed)


class PersistedRegistry:
    """A JSON-file-backed registry with a debounced, crash-safe writer.

    Shared by `DeviceRegistry` and `BLERegistry`, which had the same two
    problems:

    * They persisted by truncating the file and writing in place. A container
      kill mid-write leaves a truncated file, and a truncated registry loads as
      EMPTY — every device then re-signs-up and is issued fresh MQTT
      credentials, the worst failure mode in the add-on. Every write here goes
      through `atomic_write_json` (temp file + fsync + rename).
    * Every state report, MQTT property post and HA command re-serialised the
      whole registry and fsynced it on the event loop. `mark_dirty()` coalesces
      those into at most one write per `flush_interval`, and that write happens
      in a worker thread.

    Subclasses own their storage dict and implement `_serialize()` /
    `_restore()`; this class owns the file, the dirty flag and the flusher.
    """

    #: Longest a coalesced change may sit in memory before it reaches disk.
    FLUSH_INTERVAL = 5.0

    #: Used in log lines, so a message names which registry it is about.
    _label = "registry"

    def __init__(self, persist_path: str | Path | None = None, *,
                 flush_interval: float | None = None) -> None:
        """Load from `persist_path` if it already exists.

        Args:
            persist_path: None makes the registry memory-only — `save()` and
                `mark_dirty()` become no-ops, which is what the tests rely on.
            flush_interval: Overrides `FLUSH_INTERVAL`; tests use a small value
                so a debounced write is observable without a real 5s wait.
        """
        self._persist_path = Path(persist_path) if persist_path else None
        self._flush_interval = self.FLUSH_INTERVAL if flush_interval is None else flush_interval
        self._dirty = False
        self._flush_task: asyncio.Task[None] | None = None
        self._flush_loop: asyncio.AbstractEventLoop | None = None
        # Said once at startup, because the answer is the first thing anybody
        # needs when state that should have survived a restart did not. It is
        # also the only place the difference between "persisting" and "not"
        # is visible at all -- everything downstream behaves identically until
        # the container is replaced.
        if self._persist_path:
            log.info("%s persists to %s", self._label, self._persist_path)
        else:
            log.warning("%s is IN MEMORY ONLY -- nothing it holds survives a restart",
                        self._label)
        if self._persist_path and self._persist_path.exists():
            self._load()

    # --- subclass hooks ----------------------------------------------------

    def _serialize(self) -> Any:
        """Return the JSON-serialisable snapshot to write."""
        raise NotImplementedError

    def _restore(self, data: Any) -> None:
        """Populate the registry from a decoded JSON document."""
        raise NotImplementedError

    # --- persistence -------------------------------------------------------

    def save(self) -> None:
        """Write the registry to disk NOW, atomically.

        Kept synchronous on purpose: callers that must not lose the change even
        if the container dies a millisecond later (device signup, which mints
        MQTT credentials) rely on it, as does shutdown. Hot paths should call
        `mark_dirty()` instead.
        """
        if not self._persist_path:
            # Loud, and not `debug`. Everything this registry holds that the
            # device cannot tell us again lives or dies by this file --
            # `active_patchers` most visibly, since a device re-registers with
            # its own id and looks entirely healthy while our record of what
            # was patched onto it is gone. Silence here made "all my patchers
            # say not applied after an update" impossible to diagnose from the
            # log, which is the first place anybody looks.
            log.warning("%s is NOT being persisted: no storage path is configured. "
                        "Everything it holds is lost on restart.", self._label)
            self._dirty = False
            return
        payload = self._serialize()
        self._dirty = False
        try:
            atomic_write_json(self._persist_path, payload)
        except OSError as e:
            # A full or read-only /data must not turn into an HTTP 500 on the
            # device-facing path; keep the change queued for the next attempt.
            self._dirty = True
            log.warning("Could not persist %s to %s: %s", self._label, self._persist_path, e)

    def mark_dirty(self) -> None:
        """Note that in-memory state diverged from disk, to be written soon.

        Safe to call from sync code: when no event loop is running nothing
        would ever pick the flag up, so this degrades to an immediate `save()`.
        """
        if not self._persist_path:
            return
        self._dirty = True
        if not self._ensure_flusher():
            self.save()

    async def flush(self) -> None:
        """Write pending changes, off the event loop. No-op when clean.

        The JSON is ENCODED on the loop thread and only the finished text
        crosses into the worker. That is the whole point, not an optimisation:
        `_serialize()` returns structures that share live objects with the
        registry — `Device.to_dict()` hands back `self.config` itself, and
        `BLEDevice.to_dict()` its `state` — so encoding in the worker meant
        `json.dumps` walking dicts that request handlers were still mutating
        (`dev_device_info` writes `k3Config` through the shared dict on
        purpose, `_sync_settings_from_device` writes on every property post).
        A dict that changes size mid-encode raises `RuntimeError`, which is not
        an `OSError`, so it escaped the handler below, killed the flusher task
        and silently dropped that write window. Encoding here cannot interleave
        with anything, because there is no await inside it.
        """
        if not self._dirty or not self._persist_path:
            return
        try:
            text = json.dumps(self._serialize(), indent=2)
        except (TypeError, ValueError) as e:
            # Not retryable: the same object would fail again on every pass.
            self._dirty = False
            log.error("Could not serialise %s - not persisting: %s", self._label, e)
            return
        self._dirty = False
        try:
            await asyncio.to_thread(atomic_write_text, self._persist_path, text)
        except OSError as e:
            self._dirty = True
            log.warning("Could not persist %s to %s: %s", self._label, self._persist_path, e)

    async def start(self) -> None:
        """Start the debounced background writer. Idempotent."""
        self._ensure_flusher()

    async def stop(self) -> None:
        """Stop the background writer, flushing anything still pending.

        Without the final flush the debounce would trade the crash-safety that
        `atomic_write_json` just bought us for lost writes at every restart.
        """
        task, self._flush_task = self._flush_task, None
        self._flush_loop = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self._dirty:
            self.save()

    def _ensure_flusher(self) -> bool:
        """Start the flusher task if an event loop is running.

        Returns:
            True if a flusher is now running on the current loop and will pick
            the dirty flag up; False if there is no running loop (module import,
            startup before `web.run_app`, sync tests).
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        task = self._flush_task
        if task is not None and not task.done() and self._flush_loop is loop:
            return True
        # A task left over from another (already closed) loop can never be
        # awaited again, so drop it rather than trusting it to flush.
        self._flush_task = loop.create_task(self._flush_run())
        self._flush_task.add_done_callback(self._on_flusher_done)
        self._flush_loop = loop
        return True

    async def _flush_run(self) -> None:
        """Flush at a fixed interval, forever, until cancelled.

        Interval-driven rather than event-driven so a burst of state reports
        costs one write, not one per report.
        """
        while True:
            await asyncio.sleep(self._flush_interval)
            await self.flush()

    def _on_flusher_done(self, task: asyncio.Task[None]) -> None:
        """Write anything still pending once the flusher stops, for any reason.

        This cannot live in an `except CancelledError` inside `_flush_run`: a
        task cancelled before its first step never enters its own body, so the
        "nothing pending is lost at shutdown" guarantee has to be made out here,
        where every way the task can end is visible.
        """
        if not task.cancelled() and task.exception() is not None:
            log.error("%s writer stopped unexpectedly: %s", self._label, task.exception())
        if self._dirty:
            self.save()

    def _load(self) -> None:
        """Restore from the persisted file, or start empty and say so loudly."""
        data = read_json(self._persist_path, _UNREADABLE)
        if data is _UNREADABLE:
            # Only reached when the file exists but does not parse (read_json
            # logged the reason). Loud, because starting empty means every
            # device re-registers and is issued fresh MQTT credentials.
            log.error("%s at %s is unreadable - starting empty, devices will re-register",
                      self._label.capitalize(), self._persist_path)
            return
        self._restore(data)


class DeviceRegistry(PersistedRegistry):
    """Every WiFi device we have ever seen, keyed by its PetKit id.

    The single source of truth for device identity: the MQTT credentials minted
    in `Device.__init__` live only here and in `devices.json`, so an entry is
    never removed and never recreated for an id that already exists — a device
    that came back with different credentials would be locked out of the broker.
    `petkit_id` 0 is not a device (it is the id an unidentified request carries)
    and is dropped on load.

    Registration is written through synchronously; every later mutation goes
    through the inherited debounced writer.
    """

    _label = "device registry"

    def __init__(self, persist_path: str | Path | None = None, *,
                 flush_interval: float | None = None) -> None:
        """See `PersistedRegistry.__init__` for the arguments."""
        self._devices: dict[int, Device] = {}
        super().__init__(persist_path, flush_interval=flush_interval)

    def get(self, petkit_id: int) -> Device | None:
        """The device with this id, or None if it has never signed up."""
        return self._devices.get(petkit_id)

    def get_or_create(self, petkit_id: int, device_type: str, **kwargs: Any) -> Device:
        """Return the device for `petkit_id`, registering it on first contact.

        For a device already known, `kwargs` only FILLS IN blanks (serial
        number, MAC) and updates the firmware version; nothing else is
        overwritten, because a re-signup must not be able to rewrite an
        identity the broker has already issued credentials against.

        Args:
            kwargs: Passed straight to `Device` on creation — `serial_number`,
                `mac`, `firmware`.
        """
        device = self._devices.get(petkit_id)
        if device:
            changed = False
            if kwargs.get("serial_number") and not device.serial_number:
                device.serial_number = kwargs["serial_number"]
                device.mqtt_device_name = f"d_{device.device_type}_{device.serial_number}"
                changed = True
            if kwargs.get("mac") and not device.mac:
                device.mac = kwargs["mac"]
                changed = True
            if kwargs.get("firmware") and kwargs["firmware"] != device.firmware:
                device.firmware = kwargs["firmware"]
                changed = True
            if changed:
                # Marked dirty here, not left to the next writer: without this
                # the update lives in memory until some unrelated later save()
                # happens to flush it, and a restart in between loses a
                # firmware upgrade or a changed MAC.
                self.mark_dirty()
            return device
        device = Device(device_type=device_type, petkit_id=petkit_id, **kwargs)
        _merge_default_settings(device)
        self._devices[petkit_id] = device
        log.info("New device registered: type=%s id=%d sn=%s", device_type, petkit_id, device.serial_number)
        if device.is_ble_only:
            # Registered anyway, never refused: a device is never told no (see
            # `handle_catchall`), and refusing here would leave whatever really
            # made this request with no answer at all.
            #
            # But it should not have been possible. These models have no radio
            # that can reach us; they pair over BLE to a WiFi device that relays
            # for them. So either the codename in the URL is not the model, or
            # `DEVICE_TYPES_BLE_ONLY` is wrong about this one — and if it is,
            # this line is how we find out instead of the entity list quietly
            # being the wrong one.
            log.warning(
                "%s (id=%d) registered over the network, but %s is a BLE-only "
                "model with no WiFi of its own — it should reach us relayed "
                "through a litter box or feeder, not directly. Please report "
                "this, with the device's product name.",
                device_type, petkit_id, device_type)
        # Not debounced: this is where MQTT credentials are minted, and losing
        # them means the device reconnects with credentials we no longer know.
        self.save()
        return device

    def all(self) -> list[Device]:
        """A snapshot list of every device, safe to iterate while mutating."""
        return list(self._devices.values())

    def by_mqtt_name(self, product_key: str, device_name: str) -> Device | None:
        """Find a device by its MQTT identity, as seen in a broker client id."""
        for d in self._devices.values():
            if d.mqtt_product_key == product_key and d.mqtt_device_name == device_name:
                return d
        return None

    def by_serial(self, serial_number: str) -> Device | None:
        """Find a device by serial number; an empty serial never matches."""
        if not serial_number:
            return None
        for d in self._devices.values():
            if d.serial_number == serial_number:
                return d
        return None

    def remove(self, petkit_id: int) -> Device | None:
        """Delete a device permanently. Returns it, or None if not found."""
        device = self._devices.pop(petkit_id, None)
        if device is not None:
            log.info("Device removed: type=%s id=%d sn=%s",
                     device.device_type, petkit_id, device.serial_number)
            self.save()
        return device

    def _serialize(self) -> dict[str, Any]:
        """`{"<petkit_id>": Device.to_dict()}` — the shape of devices.json."""
        return {str(pid): d.to_dict() for pid, d in self._devices.items()}

    def _restore(self, data: Any) -> None:
        """Load `_serialize`'s shape, skipping entries that cannot be read."""
        if not isinstance(data, dict):
            log.error("Device registry at %s is not a JSON object - starting empty",
                      self._persist_path)
            return
        changed = False
        for key, d_data in data.items():
            try:
                device = Device.from_dict(d_data)
            except (AttributeError, KeyError, TypeError, ValueError) as e:
                # Per entry, not per file: one malformed record must not cost
                # every other device its credentials.
                log.warning("Skipping unreadable device entry %r: %s", key, e)
                continue
            if device.petkit_id <= 0:
                continue  # drop phantom id=0 devices (created before the fix)
            if _merge_default_settings(device):
                changed = True
            self._devices[device.petkit_id] = device
        if changed:
            # Persist the one-time cleanup now rather than with the next
            # unrelated write; a restart in between would only repeat it.
            self.mark_dirty()
        log.info("Registry loaded: %d devices", len(self._devices))
