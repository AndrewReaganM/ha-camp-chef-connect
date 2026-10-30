"""Config flow for Camp Chef Connect (email + emailed PIN)."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_TOKEN
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import CampChefApi, CampChefAuthError, CampChefConnectionError
from .const import CONF_PIN, DOMAIN

_LOGGER = logging.getLogger(__name__)


class CampChefConfigFlow(ConfigFlow, domain=DOMAIN):
    """Log in with the same email-PIN flow as the Camp Chef Connect app."""

    VERSION = 1

    def __init__(self) -> None:
        self._email: str | None = None

    def _api(self) -> CampChefApi:
        return CampChefApi(async_get_clientsession(self.hass))

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            email = user_input[CONF_EMAIL].strip().lower()
            if (error := await self._send_pin(email)) is None:
                return await self.async_step_pin()
            errors["base"] = error
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_EMAIL, default=self._email or ""): str}),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        self._email = entry_data[CONF_EMAIL]
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            assert self._email
            if (error := await self._send_pin(self._email)) is None:
                return await self.async_step_pin()
            errors["base"] = error
        return self.async_show_form(
            step_id="reauth_confirm",
            description_placeholders={"email": self._email or ""},
            errors=errors,
        )

    async def async_step_pin(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        assert self._email
        if user_input is not None:
            api = self._api()
            try:
                token = await api.login_with_pin(self._email, user_input[CONF_PIN].strip())
                user = await api.get_self()
                grills = await api.get_grills()
            except CampChefAuthError:
                errors["base"] = "invalid_pin"
            except CampChefConnectionError:
                _LOGGER.exception("Camp Chef login failed")
                errors["base"] = "cannot_connect"
            else:
                data = {CONF_EMAIL: self._email, CONF_TOKEN: token}
                await self.async_set_unique_id(str(user.get("_id") or self._email))
                if self.source == SOURCE_REAUTH:
                    self._abort_if_unique_id_mismatch(reason="wrong_account")
                    return self.async_update_reload_and_abort(
                        self._get_reauth_entry(), data=data
                    )
                self._abort_if_unique_id_configured()
                if not grills:
                    return self.async_abort(reason="no_grills")
                return self.async_create_entry(title=self._email, data=data)
        return self.async_show_form(
            step_id="pin",
            data_schema=vol.Schema({vol.Required(CONF_PIN): str}),
            description_placeholders={"email": self._email},
            errors=errors,
        )

    async def _send_pin(self, email: str) -> str | None:
        self._email = email
        try:
            await self._api().request_pin(email)
        except CampChefConnectionError:
            _LOGGER.exception("Could not request a Camp Chef login PIN")
            return "cannot_connect"
        except CampChefAuthError:
            return "unknown_account"
        return None
