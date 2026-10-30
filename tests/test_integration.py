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


async def test_never_touches_cloud_cook_goal(hass: HomeAssistant, cloud) -> None:
    await _setup(hass)
    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": CLIMATE, "temperature": 250}, blocking=True
    )
    await _wait_for_write(hass, cloud, 1)
    assert cloud.grill_patches == []


async def test_optimistic_update(hass: HomeAssistant, cloud) -> None:
    await _setup(hass)
    cloud.grill.hold_confirmations = True

    await hass.services.async_call(
        "climate", "set_temperature", {"entity_id": CLIMATE, "temperature": 240}, blocking=True
    )
    # The grill has not confirmed anything, but HA already shows the request.
    assert hass.states.get(CLIMATE).attributes["temperature"] == 240
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.woodwind_smoke_level", "value": 6},
        blocking=True,
    )
    assert hass.states.get("number.woodwind_smoke_level").state == "6"
    assert hass.states.get(CLIMATE).attributes["temperature"] == 240


async def test_offline_announcement_and_return(hass: HomeAssistant, cloud) -> None:
    await _setup(hass)
    assert hass.states.get(CLIMATE).state == HVACMode.HEAT

    await cloud.set_online(False)
    await _until(hass, lambda: hass.states.get(CLIMATE).state == "unavailable")
    assert hass.states.get("binary_sensor.woodwind_connected").state == "off"

    cloud.reads.clear()
    cloud.grill.values[6] = (180).to_bytes(2, "big")
    await cloud.set_online(True)
    # Coming back triggers a full re-read without waiting for the next poll.
    await _until(hass, lambda: hass.states.get(CLIMATE).state == HVACMode.HEAT)
    assert 2 in cloud.reads  # model is re-read too
    assert hass.states.get(CLIMATE).attributes["current_temperature"] == 180


async def test_brief_socket_drop_stays_available(hass: HomeAssistant, cloud) -> None:
    await _setup(hass)
    opens = len(cloud.ws_open_messages)
    await cloud.drop_sockets()
    await hass.async_block_till_done()
    assert hass.states.get(CLIMATE).state == HVACMode.HEAT
    assert hass.states.get("binary_sensor.woodwind_connected").state == "on"
    await _until(hass, lambda: len(cloud.ws_open_messages) > opens, timeout=10)


async def test_setup_with_grill_offline(hass: HomeAssistant, cloud) -> None:
    cloud.grill.online = False
    loop = asyncio.get_running_loop()
    started = loop.time()
    await _setup(hass)
    # No 15 s wait for a model report that will not come.
    assert loop.time() - started < 5
    await hass.async_block_till_done()
    assert hass.states.get(CLIMATE).state == "unavailable"
    assert hass.states.get("binary_sensor.woodwind_connected").state == "off"
    assert hass.states.get("sensor.woodwind_probe_4") is not None  # model from the cloud

    await _until(hass, lambda: cloud.live is not None)
    await cloud.set_online(True)
    await _until(hass, lambda: hass.states.get(CLIMATE).state == HVACMode.HEAT)


async def _until(hass: HomeAssistant, predicate, timeout: float = 5) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not predicate():
        if loop.time() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)
        await hass.async_block_till_done()
