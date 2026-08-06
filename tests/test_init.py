"""Test Aseko Local setup process."""

from datetime import timedelta

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from typing import Any

from custom_components.aseko_local import (
    _multi_enqueue,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.aseko_local.aseko_data import AsekoDevice
from custom_components.aseko_local.aseko_dumper import AsekoFrameDumper
from custom_components.aseko_local.const import (
    CONF_DEV_FORWARD_ENABLED,
    CONF_DEV_FORWARD_HOST,
    CONF_DEV_FORWARD_PORT,
    CONF_DEV_FORWARD_UNTIL,
    CONF_FORWARDER_ENABLED,
    CONF_FORWARDER_HOST,
    CONF_LOG_DUMPER_ENABLED,
    DOMAIN,
)

from .const import MOCK_CONFIG
import asyncio
from custom_components.aseko_local.mirror_forwarder import AsekoCloudMirror
from custom_components.aseko_local.aseko_server import (
    AsekoDeviceServer,
)


# We can pass fixtures as defined in conftest.py to tell pytest to use the fixture
# for a given test. We can also leverage fixtures and mocks that are available in
# Home Assistant using the pytest_homeassistant_custom_component plugin.
# Assertions allow you to verify that the return value of whatever is on the left
# side of the assertion matches with the right side.
async def test_setup_unload_entry(hass, bypass_get_data, api_server_running) -> None:
    """Test entry setup and unload."""

    # Create a mock entry so we don't have to go through config flow
    config_entry = MockConfigEntry(
        domain=DOMAIN, data=MOCK_CONFIG, entry_id="test", state=ConfigEntryState.LOADED
    )

    # Set up the entry and assert that the values set during setup are where we expect
    # them to be. Because we have patched the AsekoLocalDataUpdateCoordinator.async_get_data
    # call, no code from custom_components/aseko_local/aseko_server.py actually runs.
    assert await async_setup_entry(hass, config_entry)
    assert await async_unload_entry(hass, config_entry)

    class DummyWriter:
        def __init__(self) -> None:
            self.data = []
            self.closed = False

        def write(self, frame) -> None:
            self.data.append(frame)

        async def drain(self) -> None:
            pass

        def close(self) -> None:
            self.closed = True

        async def wait_closed(self) -> None:
            pass

    @pytest.mark.asyncio
    async def test_mirror_forwarder_enqueue_and_worker(monkeypatch) -> None:
        """Test that AsekoCloudMirror enqueues and sends frames."""

        async def dummy_open_connection(host, port) -> tuple[None, DummyWriter]:
            return None, DummyWriter()

        monkeypatch.setattr(asyncio, "open_connection", dummy_open_connection)

        mirror = AsekoCloudMirror("localhost", 12345)
        await mirror.start()
        frame = b"\x01" * 120
        await mirror.enqueue(frame)
        await asyncio.sleep(0.1)
        await mirror.stop()
        # The DummyWriter stores frames in .data
        assert any(f == frame for f in getattr(mirror._writer, "data", []))

    @pytest.mark.asyncio
    async def test_server_forward_callback(monkeypatch) -> None:
        """Test that AsekoDeviceServer forwards frames using the callback."""
        called = {}

        async def forward_cb(data) -> None:
            called["frame"] = data

        class DummyServer:
            def is_serving(self) -> bool:
                return True

            def close(self) -> None:
                pass

            async def wait_closed(self) -> None:
                pass

        # Patch start_server to avoid opening real sockets
        async def dummy_start_server(handler, host, port) -> DummyServer:
            return DummyServer()

        monkeypatch.setattr(asyncio, "start_server", dummy_start_server)

        server = await AsekoDeviceServer.create(host="127.0.0.1", port=12345)
        server.set_forward_callback(forward_cb)
        frame = b"\x02" * 120
        await server._call_forward_cb(frame)  # noqa: SLF001
        assert called["frame"] == frame

    @pytest.mark.asyncio
    async def test_server_forward_callback_none(monkeypatch) -> None:
        """Test that removing the forward callback disables forwarding."""

        class DummyServer:
            def is_serving(self) -> bool:
                return True

            def close(self) -> None:
                pass

            async def wait_closed(self) -> None:
                pass

        async def dummy_start_server(handler, host, port) -> DummyServer:
            return DummyServer()

        monkeypatch.setattr(asyncio, "start_server", dummy_start_server)

        server = await AsekoDeviceServer.create(host="127.0.0.1", port=12346)
        server.set_forward_callback(None)
        frame = b"\x03" * 120
        # Should not raise or call anything
        await server._call_forward_cb(frame)  # noqa: SLF001


# Hilfsfunktion: Hex-String zu Bytes
def hexstr_to_bytes(s: str) -> bytes:
    return bytes.fromhex(s.replace("\n", "").replace(" ", ""))


VALID_FRAME_HEX = (
    "069187240901ffffffffffff000402da0027ffff0095ff01400149ff000006640000000000ff006c"
    "069187240903ffffffffffff480a08ffffffffffffffffff027e0149ffffffffffffffffffffffea"
    "069187240902ffffffffffff0001003cffff003cffff010383ff00781e02581e28ffffffff0049a9"
)
VALID_FRAME = hexstr_to_bytes(VALID_FRAME_HEX[:240])  # MESSAGE_SIZE = 120


@pytest.mark.asyncio
async def test_device_recognition(monkeypatch) -> None:
    """Test: First frame creates new device, second frame is recognized as known."""

    await AsekoDeviceServer.remove_all()
    devices = {}

    async def on_data(device: AsekoDevice) -> None:
        # Save device by serial number
        devices[device.serial_number] = device

    class DummyWriter:
        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            pass

        def get_extra_info(self, name: str) -> Any:
            if name == "peername":
                return ("127.0.0.1", 12345)
            return None

    class DummyServer:
        def is_serving(self) -> bool:
            return True

        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            pass

    async def dummy_start_server(handler, host, port) -> DummyServer:
        reader = asyncio.StreamReader()
        writer = DummyWriter()
        # Send two valid frames
        reader.feed_data(VALID_FRAME)
        reader.feed_data(VALID_FRAME)
        reader.feed_eof()
        await handler(reader, writer)
        return DummyServer()

    monkeypatch.setattr(asyncio, "start_server", dummy_start_server)

    server = await AsekoDeviceServer.create(
        host="127.0.0.1", port=12345, on_data=on_data
    )
    assert server.running
    # It should have recognized one device
    assert len(devices) == 1
    assert 110200612 in devices  # Example serial number from frame
    await server.stop()


# ---------------------------------------------------------------------------
# Multi-device listener tests (fix for issue #99)
# ---------------------------------------------------------------------------


def _make_device(serial: int) -> AsekoDevice:
    """Return a minimal AsekoDevice with the given serial number."""
    from custom_components.aseko_local.aseko_data import AsekoDeviceType

    device = AsekoDevice()
    device.serial_number = serial
    device.device_type = AsekoDeviceType.NET
    return device


@pytest.mark.asyncio
async def test_coordinator_new_device_listener_called_for_new_device(hass) -> None:
    """Listener is called exactly once when a brand-new device arrives."""
    from custom_components.aseko_local.coordinator import (
        AsekoLocalDataUpdateCoordinator,
    )
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.aseko_local.const import DOMAIN
    from tests.const import MOCK_CONFIG

    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="test_listener")
    entry.add_to_hass(hass)

    coordinator = AsekoLocalDataUpdateCoordinator(hass, entry)
    discovered: list[AsekoDevice] = []

    unsub = coordinator.async_add_new_device_listener(discovered.append)

    device = _make_device(111)
    coordinator.devices_update_callback(device)

    assert len(discovered) == 1
    assert discovered[0].serial_number == 111

    # Second update for the SAME device must NOT trigger the listener again
    coordinator.devices_update_callback(device)
    assert len(discovered) == 1

    unsub()


@pytest.mark.asyncio
async def test_coordinator_new_device_listener_unsub(hass) -> None:
    """Unsubscribing the listener stops it from receiving future discoveries."""
    from custom_components.aseko_local.coordinator import (
        AsekoLocalDataUpdateCoordinator,
    )
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.aseko_local.const import DOMAIN
    from tests.const import MOCK_CONFIG

    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="test_unsub")
    entry.add_to_hass(hass)

    coordinator = AsekoLocalDataUpdateCoordinator(hass, entry)
    discovered: list[AsekoDevice] = []

    unsub = coordinator.async_add_new_device_listener(discovered.append)
    unsub()  # unsubscribe immediately

    coordinator.devices_update_callback(_make_device(222))
    assert len(discovered) == 0


@pytest.mark.asyncio
async def test_coordinator_multiple_listeners(hass) -> None:
    """All registered listeners receive the new-device notification."""
    from custom_components.aseko_local.coordinator import (
        AsekoLocalDataUpdateCoordinator,
    )
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.aseko_local.const import DOMAIN
    from tests.const import MOCK_CONFIG

    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG, entry_id="test_multi_cb")
    entry.add_to_hass(hass)

    coordinator = AsekoLocalDataUpdateCoordinator(hass, entry)
    bucket_a: list[int] = []
    bucket_b: list[int] = []

    unsub_a = coordinator.async_add_new_device_listener(
        lambda d: bucket_a.append(d.serial_number)
    )
    unsub_b = coordinator.async_add_new_device_listener(
        lambda d: bucket_b.append(d.serial_number)
    )

    coordinator.devices_update_callback(_make_device(333))
    coordinator.devices_update_callback(_make_device(444))

    assert bucket_a == [333, 444]
    assert bucket_b == [333, 444]

    unsub_a()
    unsub_b()


# ---------------------------------------------------------------------------
# Issue #145 — log dumper + dev-server forwarding wiring
# ---------------------------------------------------------------------------


async def test_setup_enables_dumper_when_option_set(
    hass, bypass_get_data, api_server_running, monkeypatch, tmp_path
) -> None:
    """With the log-dumper option on, the dumper singleton is enabled."""
    monkeypatch.setenv("ASEKO_DUMP_DIR", str(tmp_path))
    AsekoFrameDumper.reset()
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CONFIG,
        entry_id="test_dumper",
        options={CONF_LOG_DUMPER_ENABLED: True},
        state=ConfigEntryState.LOADED,
    )
    config_entry.add_to_hass(hass)
    assert await async_setup_entry(hass, config_entry)
    try:
        assert AsekoFrameDumper.get().enabled is True
    finally:
        await async_unload_entry(hass, config_entry)
        AsekoFrameDumper.reset()


async def test_setup_leaves_dumper_disabled_without_option(
    hass, bypass_get_data, api_server_running, monkeypatch, tmp_path
) -> None:
    """Without the option, the dumper stays disabled."""
    monkeypatch.setenv("ASEKO_DUMP_DIR", str(tmp_path))
    AsekoFrameDumper.reset()
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CONFIG,
        entry_id="test_dumper_off",
        options={},
        state=ConfigEntryState.LOADED,
    )
    config_entry.add_to_hass(hass)
    assert await async_setup_entry(hass, config_entry)
    try:
        assert AsekoFrameDumper.get().enabled is False
    finally:
        await async_unload_entry(hass, config_entry)
        AsekoFrameDumper.reset()


async def test_setup_resets_expired_dev_forward(
    hass, bypass_get_data, api_server_running
) -> None:
    """An expired dev-forward option is reset to disabled on setup."""
    past = (dt_util.utcnow() - timedelta(hours=1)).isoformat()
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CONFIG,
        entry_id="test_dev_expired",
        options={
            CONF_DEV_FORWARD_ENABLED: True,
            CONF_DEV_FORWARD_HOST: "dev.example.com",
            CONF_DEV_FORWARD_PORT: 47524,
            CONF_DEV_FORWARD_UNTIL: past,
        },
        state=ConfigEntryState.LOADED,
    )
    config_entry.add_to_hass(hass)
    assert await async_setup_entry(hass, config_entry)
    try:
        assert config_entry.options.get(CONF_DEV_FORWARD_ENABLED) is False
        assert config_entry.options.get(CONF_DEV_FORWARD_UNTIL) is None
        assert config_entry.runtime_data.dev_mirror is None
    finally:
        await async_unload_entry(hass, config_entry)


async def test_setup_starts_dev_mirror_when_enabled(
    hass, bypass_get_data, api_server_running, monkeypatch
) -> None:
    """A valid, unexpired dev-forward option starts the dev mirror."""
    future = (dt_util.utcnow() + timedelta(hours=23)).isoformat()
    started: list[Any] = []

    async def fake_start(self) -> None:
        started.append(self)

    monkeypatch.setattr(AsekoCloudMirror, "start", fake_start)
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CONFIG,
        entry_id="test_dev_active",
        options={
            CONF_DEV_FORWARD_ENABLED: True,
            CONF_DEV_FORWARD_HOST: "dev.example.com",
            CONF_DEV_FORWARD_PORT: 47524,
            CONF_DEV_FORWARD_UNTIL: future,
        },
        state=ConfigEntryState.LOADED,
    )
    config_entry.add_to_hass(hass)
    assert await async_setup_entry(hass, config_entry)
    try:
        assert len(started) == 1
        assert config_entry.runtime_data.dev_mirror is not None
    finally:
        await async_unload_entry(hass, config_entry)


async def test_multi_enqueue_isolates_failing_target() -> None:
    """A failing target must not stop other targets from receiving frames."""
    received: list[bytes] = []

    async def good(frame: bytes) -> None:
        received.append(frame)

    def bad(frame: bytes) -> None:
        raise RuntimeError("dev mirror unreachable")

    combined = _multi_enqueue([bad, good])
    await combined(b"frame")
    assert received == [b"frame"]


async def test_dev_forward_expiry_keeps_cloud_mirror_running(
    hass, bypass_get_data, api_server_running, monkeypatch, tmp_path
) -> None:
    """24 h auto-expiry stops only the dev mirror; the cloud mirror keeps going."""
    monkeypatch.setenv("ASEKO_DUMP_DIR", str(tmp_path))
    AsekoFrameDumper.reset()

    async def fake_start(self) -> None:
        pass

    async def fake_stop(self) -> None:
        pass

    monkeypatch.setattr(AsekoCloudMirror, "start", fake_start)
    monkeypatch.setattr(AsekoCloudMirror, "stop", fake_stop)

    future = (dt_util.utcnow() + timedelta(hours=23)).isoformat()
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CONFIG,
        entry_id="test_dev_expiry_keeps_cloud",
        options={
            CONF_FORWARDER_ENABLED: True,
            CONF_FORWARDER_HOST: "pool.aseko.com",
            CONF_DEV_FORWARD_ENABLED: True,
            CONF_DEV_FORWARD_HOST: "dev.example.com",
            CONF_DEV_FORWARD_PORT: 47524,
            CONF_DEV_FORWARD_UNTIL: future,
        },
        state=ConfigEntryState.LOADED,
    )
    config_entry.add_to_hass(hass)
    assert await async_setup_entry(hass, config_entry)
    try:
        rd = config_entry.runtime_data
        assert rd.mirror is not None
        assert rd.dev_mirror is not None

        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=24))
        await hass.async_block_till_done()

        assert rd.dev_mirror is None
        assert rd.mirror is not None
        assert config_entry.options.get(CONF_DEV_FORWARD_ENABLED) is False
        assert config_entry.options.get(CONF_DEV_FORWARD_UNTIL) is None

        await rd.server._call_forward_cb(b"\x00" * 120)
        await rd.server._call_forward_v8_cb(b"{v1 " + b"\x00" * 120)
        assert not rd.mirror._queue.empty()
    finally:
        await async_unload_entry(hass, config_entry)
        AsekoFrameDumper.reset()
