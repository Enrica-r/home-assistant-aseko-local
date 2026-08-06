"""The Aseko Local integration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

import voluptuous as vol

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.event import (
    async_track_point_in_utc_time,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .aseko_data import AsekoDevice
from .aseko_dumper import AsekoFrameDumper
from .aseko_server import AsekoDeviceServer
from .const import (
    CONF_DEV_FORWARD_ENABLED,
    CONF_DEV_FORWARD_HOST,
    CONF_DEV_FORWARD_PORT,
    CONF_DEV_FORWARD_UNTIL,
    CONF_FORWARDER_ENABLED,
    CONF_FORWARDER_HOST,
    CONF_LOG_DUMPER_ENABLED,
    DEFAULT_DEV_FORWARD_PORT,
    DEFAULT_FORWARDER_PORT_V7,
    DEFAULT_FORWARDER_PORT_V8,
    DOMAIN,
    LOG_DUMPER_NOTIFY_AFTER,
    LOG_DUMPER_RETENTION,
)
from .consumption_tracker import PUMP_KEYS
from .coordinator import AsekoLocalDataUpdateCoordinator
from .mirror_forwarder import AsekoCloudMirror

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.SENSOR]

_MIRRORS: dict[str, AsekoCloudMirror] = {}
_SERVERS: dict[str, AsekoDeviceServer] = {}

SERVICE_RESET_CONSUMPTION = "reset_consumption"

RESET_CONSUMPTION_SCHEMA = vol.Schema(
    {
        vol.Optional("pump", default="all"): vol.In(list(PUMP_KEYS) + ["all"]),
        vol.Optional("counter", default="canister"): vol.In(
            ["canister", "total", "all"]
        ),
    }
)

type AsekoLocalConfigEntry = ConfigEntry["AsekoLocalRuntimeData"]


def _multi_enqueue(
    targets: list[Callable[[bytes], object]],
) -> Callable[[bytes], object]:
    """Combine several frame sinks into one callback that fans out to all."""

    async def _enqueue_all(frame: bytes) -> None:
        for target in targets:
            try:
                result = target(frame)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                _LOGGER.error("Forward target raised an exception", exc_info=True)

    return _enqueue_all


def _disable_dev_forward(hass: HomeAssistant, config_entry: ConfigEntry) -> None:
    """Persist the dev-forward option as disabled (used when it expires)."""
    options = dict(config_entry.options)
    options[CONF_DEV_FORWARD_ENABLED] = False
    options.pop(CONF_DEV_FORWARD_UNTIL, None)
    hass.config_entries.async_update_entry(config_entry, options=options)


@dataclass
class AsekoLocalRuntimeData:
    coordinator: AsekoLocalDataUpdateCoordinator
    device_discovered: bool = False
    mirror: AsekoCloudMirror | None = None
    mirror_v8: AsekoCloudMirror | None = None
    server: AsekoDeviceServer | None = None
    dev_mirror: AsekoCloudMirror | None = None
    dumper_notify_unsub: Callable[[], None] | None = None
    dumper_cleanup_unsub: Callable[[], None] | None = None
    dev_forward_unsub: Callable[[], None] | None = None


async def async_setup_entry(
    hass: HomeAssistant, config_entry: AsekoLocalConfigEntry
) -> bool:
    """Set up Aseko Local from a config entry."""

    def _snapshot_ready(dev: AsekoDevice) -> bool:
        # wait until the device has a serial number and a valid device type is available
        base_keys = ("serial_number", "device_type")
        return all(getattr(dev, k, None) is not None for k in base_keys)

    async def new_device_callback(device: AsekoDevice) -> None:
        # Protected against early calls before runtime_data is set
        rd = getattr(config_entry, "runtime_data", None)
        if not rd:
            _LOGGER.debug("Callback before runtime_data is ready – skipping.")
            return

        if rd.device_discovered:
            return

        if not _snapshot_ready(device):
            _LOGGER.debug("Deferring platform setup; first snapshot not ready yet.")
            return

        rd.device_discovered = True  # set early to prevent concurrent calls
        try:
            await hass.config_entries.async_forward_entry_setups(
                config_entry, PLATFORMS
            )
        except Exception:
            rd.device_discovered = False  # allow retry on next device callback
            raise
        # Load persisted backwash timestamps for known devices (e.g. after
        # an HA restart, so the sensor shows the last observed value
        # immediately on first frame).
        await rd.coordinator.async_setup_backwash_trackers()
        _LOGGER.info("New Aseko device registered: %s", device.serial_number)

    coordinator = AsekoLocalDataUpdateCoordinator(
        hass, config_entry, new_device_callback
    )

    config_entry.runtime_data = AsekoLocalRuntimeData(
        coordinator=coordinator,
        device_discovered=False,
        mirror=None,
        mirror_v8=None,
        server=None,
        dev_mirror=None,
        dumper_notify_unsub=None,
        dumper_cleanup_unsub=None,
        dev_forward_unsub=None,
    )

    # Raw-Sink: caches the last frame per device for diagnostics; the same
    # callbacks feed the (optional) log dumper.
    dumper = AsekoFrameDumper.get()

    def _dump_sink(base_sink: Callable[[bytes], None]) -> Callable[[bytes], None]:
        def sink(raw: bytes) -> None:
            base_sink(raw)
            dumper.record(raw)

        return sink

    raw_sink = _dump_sink(coordinator.store_raw_frame)
    v8_raw_sink = _dump_sink(coordinator.store_v8_frame)

    # start Server
    server = await AsekoDeviceServer.create(
        host=config_entry.data[CONF_HOST],
        port=config_entry.data[CONF_PORT],
        on_data=coordinator.devices_update_callback,
        raw_sink=raw_sink,
        v8_raw_sink=v8_raw_sink,
    )

    if not server.running:
        raise ConfigEntryNotReady

    coordinator.async_start_stale_check()

    rd = config_entry.runtime_data

    # --- Log dumper (Issue #145) -----------------------------------------
    # One checkbox across all entries: the dumper is active if ANY entry has
    # it enabled (a device uses either the v7 or the v8 decoder).
    dumper.set_enabled(
        any(
            entry.options.get(CONF_LOG_DUMPER_ENABLED, False)
            for entry in hass.config_entries.async_entries(DOMAIN)
        )
    )

    def _cleanup_dumps(_now: object) -> None:
        dumper.cleanup(LOG_DUMPER_RETENTION)

    def _notify_dumper_still_on(_now: object) -> None:
        if not dumper.enabled:
            return
        _LOGGER.warning(
            "Aseko log dumper is still active after %s. Disable it in the "
            "integration options once you have collected the data.",
            LOG_DUMPER_NOTIFY_AFTER,
        )
        persistent_notification.async_create(
            hass,
            title="Aseko Local: Log Dumper still active",
            message=(
                "The log dumper has been active for more than "
                f"{LOG_DUMPER_NOTIFY_AFTER.days} days. Please switch it off in "
                "Settings → Devices & Services → Aseko Local → Options after "
                "collecting the data. Dump files are removed automatically two "
                "days after the last write."
            ),
            notification_id="aseko_local_log_dumper_active",
        )

    # Rolling deletion runs regardless of the checkbox so leftover files from
    # a previous enable expire on their own (2-day retention).
    dumper.cleanup(LOG_DUMPER_RETENTION)
    rd.dumper_cleanup_unsub = async_track_time_interval(
        hass, _cleanup_dumps, timedelta(days=1)
    )

    if dumper.enabled:
        # Remind the user once the dumper has been on for a while.
        rd.dumper_notify_unsub = async_track_point_in_utc_time(
            hass, _notify_dumper_still_on, dt_util.utcnow() + LOG_DUMPER_NOTIFY_AFTER
        )

    # --- Forwarding targets: cloud mirror + optional dev server -----------
    v7_targets: list[Callable[[bytes], object]] = []
    v8_targets: list[Callable[[bytes], object]] = []
    mirror_instance = None
    mirror_v8_instance = None
    dev_mirror = None

    def _expire_dev_forward(_now: object) -> None:
        _LOGGER.warning(
            "Dev-server forwarding has been active for 24 h; disabling it now."
        )
        # The time tracker normally invokes this on the event loop, but we
        # always hop back onto the loop so the config update is guaranteed to
        # run on the loop thread no matter how the callback was triggered.
        hass.loop.call_soon_threadsafe(_apply_dev_forward_expiry)

    def _apply_dev_forward_expiry() -> None:
        _disable_dev_forward(hass, config_entry)
        current = getattr(config_entry, "runtime_data", None)
        if current is not None and current.dev_mirror is not None:
            stopped = current.dev_mirror
            current.dev_mirror = None
            # Drop the dev mirror from the fan-out targets so the cloud
            # mirror keeps working without a dead target attached.
            for targets in (v7_targets, v8_targets):
                try:
                    targets.remove(stopped.enqueue)
                except ValueError:
                    pass
            hass.async_create_task(stopped.stop())
            if v7_targets:
                server.set_forward_callback(_multi_enqueue(v7_targets))
            else:
                server.set_forward_callback(None)
            if v8_targets:
                server.set_forward_v8_callback(_multi_enqueue(v8_targets))
            else:
                server.set_forward_v8_callback(None)
        persistent_notification.async_create(
            hass,
            title="Aseko Local: Dev-server forwarding disabled",
            message=(
                "Forwarding to the development server was automatically disabled "
                "after 24 hours. Re-enable it in the integration options if you "
                "need more data."
            ),
            notification_id="aseko_local_dev_forward_expired",
        )

    if config_entry.options.get(CONF_FORWARDER_ENABLED):
        forwarder_host = config_entry.options.get(CONF_FORWARDER_HOST)
        if forwarder_host:
            mirror_instance = AsekoCloudMirror(
                cloud_host=forwarder_host, cloud_port=DEFAULT_FORWARDER_PORT_V7
            )
            await mirror_instance.start()
            v7_targets.append(mirror_instance.enqueue)

            mirror_v8_instance = AsekoCloudMirror(
                cloud_host=forwarder_host, cloud_port=DEFAULT_FORWARDER_PORT_V8
            )
            await mirror_v8_instance.start()
            v8_targets.append(mirror_v8_instance.enqueue)

            _LOGGER.info(
                "Cloud forwarding enabled to %s (v7:%d, v8:%d)",
                forwarder_host,
                DEFAULT_FORWARDER_PORT_V7,
                DEFAULT_FORWARDER_PORT_V8,
            )
        else:
            _LOGGER.warning("Forwarder enabled but host not set — skipping mirror.")

    # Optional: raw frames to a dev server. Opt-in, hard 24 h cap enforced on
    # setup (covers HA restarts) and by a timer scheduled at the stored expiry.
    if config_entry.options.get(CONF_DEV_FORWARD_ENABLED):
        until_raw = config_entry.options.get(CONF_DEV_FORWARD_UNTIL)
        until_dt = dt_util.parse_datetime(until_raw) if until_raw else None
        if until_dt is None or dt_util.utcnow() >= dt_util.as_utc(until_dt):
            _disable_dev_forward(hass, config_entry)
            _LOGGER.warning(
                "Dev-server forwarding has expired; option reset to disabled."
            )
        else:
            dev_host = config_entry.options.get(CONF_DEV_FORWARD_HOST)
            if dev_host:
                dev_mirror = AsekoCloudMirror(
                    cloud_host=dev_host,
                    cloud_port=config_entry.options.get(
                        CONF_DEV_FORWARD_PORT, DEFAULT_DEV_FORWARD_PORT
                    ),
                )
                await dev_mirror.start()
                v7_targets.append(dev_mirror.enqueue)
                v8_targets.append(dev_mirror.enqueue)
                rd.dev_forward_unsub = async_track_point_in_utc_time(
                    hass, _expire_dev_forward, dt_util.as_utc(until_dt)
                )
                _LOGGER.info(
                    "Dev-server forwarding enabled to %s:%s until %s",
                    dev_host,
                    config_entry.options.get(CONF_DEV_FORWARD_PORT),
                    until_dt,
                )
            else:
                _LOGGER.warning("Dev forwarding enabled but host not set — skipping.")

    if v7_targets:
        server.set_forward_callback(_multi_enqueue(v7_targets))
    if v8_targets:
        server.set_forward_v8_callback(_multi_enqueue(v8_targets))

    # Add to runtime_data
    rd.server = server
    rd.mirror = mirror_instance
    rd.mirror_v8 = mirror_v8_instance
    rd.dev_mirror = dev_mirror

    # Register domain service once (shared across all config entries)
    if not hass.services.has_service(DOMAIN, SERVICE_RESET_CONSUMPTION):

        async def handle_reset_consumption(call: ServiceCall) -> None:
            pump = call.data.get("pump", "all")
            counter = call.data.get("counter", "canister")
            for entry in hass.config_entries.async_entries(DOMAIN):
                rd = getattr(entry, "runtime_data", None)
                if rd:
                    rd.coordinator.reset_consumption(pump, counter)

        hass.services.async_register(
            DOMAIN,
            SERVICE_RESET_CONSUMPTION,
            handle_reset_consumption,
            schema=RESET_CONSUMPTION_SCHEMA,
        )
        _LOGGER.debug("Registered service %s.%s", DOMAIN, SERVICE_RESET_CONSUMPTION)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload Aseko Local config entry."""
    _LOGGER.info("Unloading Aseko Local entry %s", entry.entry_id)

    unload_ok = True

    # Unload platforms only if they were actually loaded
    if getattr(entry, "runtime_data", None) and entry.runtime_data.device_discovered:
        unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        rd = getattr(entry, "runtime_data", None)
        if rd is not None:
            # Stop server, mirrors and cancel our timers
            rd.coordinator.async_stop_stale_check()
            if rd.dumper_cleanup_unsub is not None:
                rd.dumper_cleanup_unsub()
            if rd.dumper_notify_unsub is not None:
                rd.dumper_notify_unsub()
            if rd.dev_forward_unsub is not None:
                rd.dev_forward_unsub()
            if rd.dev_mirror is not None:
                await rd.dev_mirror.stop()
            if rd.server is not None:
                await rd.server.stop()
            if rd.mirror is not None:
                await rd.mirror.stop()
            if rd.mirror_v8 is not None:
                await rd.mirror_v8.stop()

        # Keep the dumper state consistent across the remaining entries and
        # roll any dump files whose retention period elapsed.
        remaining = [
            e
            for e in hass.config_entries.async_entries(DOMAIN)
            if e.entry_id != entry.entry_id
        ]
        AsekoFrameDumper.get().set_enabled(
            any(e.options.get(CONF_LOG_DUMPER_ENABLED, False) for e in remaining)
        )
        AsekoFrameDumper.get().cleanup(LOG_DUMPER_RETENTION)

        # Remove domain service when the last entry is unloaded
        if not remaining and hass.services.has_service(
            DOMAIN, SERVICE_RESET_CONSUMPTION
        ):
            hass.services.async_remove(DOMAIN, SERVICE_RESET_CONSUMPTION)
            _LOGGER.debug(
                "Unregistered service %s.%s", DOMAIN, SERVICE_RESET_CONSUMPTION
            )

        # Remove runtime_data to avoid stale references
        domain_data = hass.data.get(DOMAIN)
        if domain_data is not None:
            domain_data.pop(entry.entry_id, None)
            if not domain_data:
                hass.data.pop(DOMAIN, None)

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle reload of the entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
