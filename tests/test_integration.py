"""Config flow and runtime tests against the fake cloud."""

from __future__ import annotations

import asyncio

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant import config_entries
from homeassistant.components.climate import HVACMode
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from custom_components.camp_chef_connect.const import DOMAIN

from .conftest import PIN, TOKEN

CLIMATE = "climate.woodwind"


async def _wait_for_write(hass: HomeAssistant, cloud, count: int) -> bytes:
    for _ in range(50):
        if len(cloud.grill.writes) >= count:
            await asyncio.sleep(0.05)  # let the read-back of MODE land
            await hass.async_block_till_done()
            return cloud.grill.writes[count - 1]
        await asyncio.sleep(0.02)
    raise AssertionError(f"expected {count} writes, got {cloud.grill.writes}")


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    hass.config.units = US_CUSTOMARY_SYSTEM
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="user-1",
        data={"email": "me@example.com", "token": TOKEN},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_config_flow(hass: HomeAssistant, cloud) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": " Me@Example.com "}
    )
    assert result["step_id"] == "pin"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"pin": "000000"})
    assert result["errors"] == {"base": "invalid_pin"}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"pin": PIN})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "me@example.com"
    assert result["data"] == {"email": "me@example.com", "token": TOKEN}
    assert result["result"].unique_id == "user-1"


async def test_entities_reflect_grill(hass: HomeAssistant, cloud) -> None:
    entry = await _setup(hass)
    assert entry.state is ConfigEntryState.LOADED

    climate = hass.states.get(CLIMATE)
    assert climate.state == HVACMode.HEAT
    assert climate.attributes["current_temperature"] == 221
    assert climate.attributes["temperature"] == 225
    assert climate.attributes["min_temp"] == 160
    assert climate.attributes["max_temp"] == 500
    assert hass.states.get("sensor.woodwind_chamber_temperature").state == "221"
    assert hass.states.get("sensor.woodwind_probe_1").state == "145"
    assert hass.states.get("sensor.woodwind_probe_2").state == "unknown"
    assert hass.states.get("sensor.woodwind_probe_4") is not None
    assert hass.states.get("sensor.woodwind_pellet_level").state == "70"
    assert hass.states.get("sensor.woodwind_mode").state == "run"
    assert hass.states.get("number.woodwind_smoke_level").state == "3"
    assert hass.states.get("binary_sensor.woodwind_connected").state == "on"
    assert hass.states.get("binary_sensor.woodwind_fault").state == "off"

    open_msg = cloud.ws_open_messages[0]
    assert open_msg["path"] == "/client-socket/grill-1"
    assert open_msg["queryParams"] == {"jwt": [TOKEN]}


async def test_set_temperature_and_smoke(hass: HomeAssistant, cloud) -> None:
    await _setup(hass)

    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": CLIMATE, "temperature": 250}, blocking=True
    )
    assert await _wait_for_write(hass, cloud, 1) == bytes([1, 2, 0x11, 0x00, 0xFA, 0x03, 0x00])

    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.woodwind_smoke_level", "value": 7},
        blocking=True,
    )
    assert await _wait_for_write(hass, cloud, 2) == bytes([1, 2, 0x11, 0x00, 0xFA, 0x07, 0x00])
    assert hass.states.get(CLIMATE).attributes["temperature"] == 250


async def test_shutdown_but_no_remote_ignition(hass: HomeAssistant, cloud) -> None:
    await _setup(hass)

    await hass.services.async_call(
        "climate", "set_hvac_mode", {"entity_id": CLIMATE, "hvac_mode": "off"}, blocking=True
    )
    assert await _wait_for_write(hass, cloud, 1) == bytes([1, 2, 0])
    assert hass.states.get(CLIMATE).state == HVACMode.OFF
    assert hass.states.get("number.woodwind_smoke_level").state == "unavailable"

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "climate", "set_hvac_mode", {"entity_id": CLIMATE, "hvac_mode": "heat"}, blocking=True
        )
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "climate", "set_temperature", {"entity_id": CLIMATE, "temperature": 250}, blocking=True
        )
    assert cloud.grill.writes == [bytes([1, 2, 0])]


async def test_socket_auth_failure_starts_reauth(hass: HomeAssistant, cloud) -> None:
    cloud.reject_socket_auth = True
    entry = await _setup(hass)
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == ["reauth"]
    assert entry.state is ConfigEntryState.LOADED


async def test_rejected_token_fails_setup_with_reauth(hass: HomeAssistant, cloud) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="user-1", data={"email": "me@example.com", "token": "bad"}
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert flows and flows[0]["context"]["source"] == "reauth"


async def test_temperature_change_updates_cloud_goal_first(hass: HomeAssistant, cloud) -> None:
    await _setup(hass)

    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": CLIMATE, "temperature": 250}, blocking=True
    )
    await _wait_for_write(hass, cloud, 1)
    assert cloud.grill_patches == [
        {
            "id": "grill-1",
            "body": {"currentSession": {"IfNotNull": {"chamberGoal": {"Assign": 250}}}},
        }
    ]
    assert cloud.events == ["patch", "write"]

    # Smoke has no cloud field, so a smoke-only change stays direct.
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.woodwind_smoke_level", "value": 6},
        blocking=True,
    )
    await _wait_for_write(hass, cloud, 2)
    assert len(cloud.grill_patches) == 1


async def test_cloud_goal_failure_still_writes(hass: HomeAssistant, cloud) -> None:
    await _setup(hass)
    cloud.fail_patch = True

    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": CLIMATE, "temperature": 240}, blocking=True
    )
    assert await _wait_for_write(hass, cloud, 1) == bytes([1, 2, 0x11, 0x00, 0xF0, 0x03, 0x00])
