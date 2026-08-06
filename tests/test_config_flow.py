"""Test the Aseko Local config flow."""

from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.aseko_local.aseko_server import ServerConnectionError
from custom_components.aseko_local.const import (
    CONF_DEV_FORWARD_ENABLED,
    CONF_DEV_FORWARD_HOST,
    CONF_DEV_FORWARD_PORT,
    CONF_DEV_FORWARD_UNTIL,
    CONF_FORWARDER_ENABLED,
    CONF_FORWARDER_HOST,
    CONF_LOG_DUMPER_ENABLED,
    DEFAULT_FORWARDER_HOST,
    DEFAULT_DEV_FORWARD_PORT,
    DOMAIN,
)


async def test_form(hass: HomeAssistant, mock_setup_entry: AsyncMock) -> None:
    """Test we get the form."""

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result.get("type") is FlowResultType.FORM
    assert result.get("errors") == {}

    with patch(
        "custom_components.aseko_local.aseko_server.AsekoDeviceServer.start",
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "1.1.1.1",
                CONF_PORT: 12345,
            },
        )
        await hass.async_block_till_done()

    assert result.get("type") is FlowResultType.CREATE_ENTRY
    assert result.get("title") == "Aseko Local - 1.1.1.1:12345"
    assert result.get("data") == {
        CONF_HOST: "1.1.1.1",
        CONF_PORT: 12345,
    }


async def test_form_cannot_connect(
    hass: HomeAssistant, mock_setup_entry: AsyncMock
) -> None:
    """Test we handle cannot connect error."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    with patch(
        "custom_components.aseko_local.aseko_server.AsekoDeviceServer.start",
        side_effect=ServerConnectionError,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "1.1.1.1",
                CONF_PORT: 12345,
            },
        )
    assert result.get("type") is FlowResultType.FORM
    assert result.get("errors") == {"base": "cannot_connect"}

    # Make sure the config flow tests finish with either an
    # FlowResultType.CREATE_ENTRY or FlowResultType.ABORT so
    # we can show the config flow is able to recover from an error.

    with patch(
        "custom_components.aseko_local.aseko_server.AsekoDeviceServer.start",
        return_value=None,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                CONF_HOST: "1.1.1.1",
                CONF_PORT: 12345,
            },
        )
        await hass.async_block_till_done()

    assert result.get("type") is FlowResultType.CREATE_ENTRY
    assert result.get("title") == "Aseko Local - 1.1.1.1:12345"
    assert result.get("data") == {
        CONF_HOST: "1.1.1.1",
        CONF_PORT: 12345,
    }
    assert len(mock_setup_entry.mock_calls) == 1


async def test_options_flow(
    hass, mock_config_entry, mock_setup_entry: AsyncMock
) -> None:
    """Test the options flow for Aseko Local."""

    # If the fixture is async:
    if callable(getattr(mock_config_entry, "__await__", None)):
        mock_config_entry = await mock_config_entry

    # Start options flow
    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "options_init"

    options = {
        CONF_FORWARDER_ENABLED: True,
        CONF_FORWARDER_HOST: DEFAULT_FORWARDER_HOST,
    }

    with patch(
        "custom_components.aseko_local.aseko_server.AsekoDeviceServer.remove_all"
    ):
        result2 = await hass.config_entries.options.async_configure(
            result["flow_id"],
            options,
        )
        await hass.async_block_till_done()

    assert result2["type"] == FlowResultType.CREATE_ENTRY
    assert result2["data"] == {
        CONF_FORWARDER_ENABLED: True,
        CONF_FORWARDER_HOST: DEFAULT_FORWARDER_HOST,
        CONF_LOG_DUMPER_ENABLED: False,
        CONF_DEV_FORWARD_ENABLED: False,
        CONF_DEV_FORWARD_HOST: "",
        CONF_DEV_FORWARD_PORT: DEFAULT_DEV_FORWARD_PORT,
    }


async def test_options_flow_saves_dumper_and_dev_forward(
    hass, mock_config_entry, mock_setup_entry: AsyncMock
) -> None:
    """Saving the options persists the dumper checkbox and the dev-forward
    settings, including the 24 h expiry timestamp for the dev-forward."""
    if callable(getattr(mock_config_entry, "__await__", None)):
        mock_config_entry = await mock_config_entry

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "options_init"

    options = {
        CONF_LOG_DUMPER_ENABLED: True,
        CONF_DEV_FORWARD_ENABLED: True,
        CONF_DEV_FORWARD_HOST: "dev.example.com",
        CONF_DEV_FORWARD_PORT: 47524,
    }

    with patch(
        "custom_components.aseko_local.aseko_server.AsekoDeviceServer.remove_all"
    ):
        result2 = await hass.config_entries.options.async_configure(
            result["flow_id"],
            options,
        )
        await hass.async_block_till_done()

    assert result2["type"] == FlowResultType.CREATE_ENTRY
    data = result2["data"]
    assert data[CONF_LOG_DUMPER_ENABLED] is True
    assert data[CONF_DEV_FORWARD_ENABLED] is True
    assert data[CONF_DEV_FORWARD_HOST] == "dev.example.com"
    assert data[CONF_DEV_FORWARD_PORT] == 47524
    # A non-empty ISO expiry must be stored when dev-forwarding is activated.
    assert isinstance(data[CONF_DEV_FORWARD_UNTIL], str)
    assert data[CONF_DEV_FORWARD_UNTIL]


async def test_options_flow_requires_dev_forward_host(
    hass, mock_config_entry, mock_setup_entry: AsyncMock
) -> None:
    """Enabling dev-forward without a host must show a validation error."""
    if callable(getattr(mock_config_entry, "__await__", None)):
        mock_config_entry = await mock_config_entry

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    options = {
        CONF_DEV_FORWARD_ENABLED: True,
        CONF_DEV_FORWARD_HOST: "",
    }

    result2 = await hass.config_entries.options.async_configure(
        result["flow_id"],
        options,
    )
    assert result2["type"] == FlowResultType.FORM
    assert result2["errors"] == {"base": "dev_forward_host_missing"}


async def test_options_flow_clears_until_when_dev_forward_disabled(
    hass, mock_config_entry, mock_setup_entry: AsyncMock
) -> None:
    """Unchecking dev-forward removes the stored expiry timestamp."""
    if callable(getattr(mock_config_entry, "__await__", None)):
        mock_config_entry = await mock_config_entry

    # First enable dev-forward so an expiry timestamp is stored.
    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    with patch(
        "custom_components.aseko_local.aseko_server.AsekoDeviceServer.remove_all"
    ):
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_DEV_FORWARD_ENABLED: True,
                CONF_DEV_FORWARD_HOST: "dev.example.com",
                CONF_DEV_FORWARD_PORT: DEFAULT_DEV_FORWARD_PORT,
            },
        )
        await hass.async_block_till_done()
    assert mock_config_entry.options.get(CONF_DEV_FORWARD_UNTIL)

    # Now disable it again — the expiry must be dropped.
    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    with patch(
        "custom_components.aseko_local.aseko_server.AsekoDeviceServer.remove_all"
    ):
        result2 = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_DEV_FORWARD_ENABLED: False},
        )
        await hass.async_block_till_done()

    assert result2["type"] == FlowResultType.CREATE_ENTRY
    assert CONF_DEV_FORWARD_UNTIL not in result2["data"]
