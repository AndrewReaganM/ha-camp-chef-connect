"""Camp Chef Connect integration (cloud relay to Camp Chef WiFi grills)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_TOKEN
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import CampChefApi, CampChefAuthError, CampChefConnectionError, token_needs_refresh
from .const import PLATFORMS
from .grill import CampChefGrill

_LOGGER = logging.getLogger(__name__)

FIRST_REPORT_WAIT = 15


@dataclass
class CampChefData:
    """Runtime data for a config entry."""

    api: CampChefApi
    grills: list[CampChefGrill]


type CampChefConfigEntry = ConfigEntry[CampChefData]


async def async_setup_entry(hass: HomeAssistant, entry: CampChefConfigEntry) -> bool:
    """Set up Camp Chef Connect from a config entry."""
    session = async_get_clientsession(hass)
    api = CampChefApi(session, entry.data[CONF_TOKEN])
    token_lock = asyncio.Lock()

    @callback
    def save_token(token: str) -> None:
        if token != entry.data.get(CONF_TOKEN):
            hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_TOKEN: token})

    async def get_token() -> str:
        async with token_lock:
            if token_needs_refresh(api.token or ""):
                try:
                    save_token(await api.refresh_token())
                except CampChefConnectionError as err:
                    _LOGGER.debug("Token refresh failed, using current token: %s", err)
            assert api.token
            return api.token

    try:
        # Refreshing on every start keeps the session sliding forward.
        save_token(await api.refresh_token())
        grill_infos = await api.get_grills()
    except CampChefAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except CampChefConnectionError as err:
        raise ConfigEntryNotReady(str(err)) from err

    @callback
    def auth_failed() -> None:
        entry.async_start_reauth(hass)

    grills = [
        CampChefGrill(hass, session, info, get_token, auth_failed)
        for info in grill_infos
        if info.get("_id")
    ]
    entry.runtime_data = CampChefData(api=api, grills=grills)

    for grill in grills:
        grill.start()
    # Give online grills a moment to report in so entities start with data.
    await asyncio.gather(
        *(grill.async_wait_for_first_report(FIRST_REPORT_WAIT) for grill in grills)
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: CampChefConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        for grill in entry.runtime_data.grills:
            await grill.stop()
    return unloaded
