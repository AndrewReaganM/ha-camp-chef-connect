"""Grill chamber as a climate entity."""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CampChefConfigEntry
from .const import DEFAULT_SMOKE, TEMP_STEP_F
from .entity import CampChefEntity
from .grill import CampChefGrill
from .protocol import GrillModeType


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CampChefConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(CampChefClimate(grill) for grill in entry.runtime_data.grills)


class CampChefClimate(CampChefEntity, ClimateEntity):
    """Chamber temperature and set point.

    The grill can be shut down remotely, but it cannot be lit remotely:
    switching to heat from off is refused so the firepot is never ignited
    without someone at the grill.
    """

    _attr_name = None
    _attr_translation_key = "grill"
    _attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
    _attr_target_temperature_step = TEMP_STEP_F
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_OFF
        | ClimateEntityFeature.TURN_ON
    )

    def __init__(self, grill: CampChefGrill) -> None:
        super().__init__(grill, "climate")

    @property
    def min_temp(self) -> float:
        return self.grill.min_temp_f

    @property
    def max_temp(self) -> float:
        return self.grill.max_temp_f

    @property
    def current_temperature(self) -> float | None:
        return self.grill.state.chamber_f

    @property
    def target_temperature(self) -> float | None:
        return self.grill.state.set_temp_f

    @property
    def hvac_mode(self) -> HVACMode | None:
        if self.grill.state.mode is None:
            return None
        return HVACMode.HEAT if self.grill.is_lit else HVACMode.OFF

    @property
    def hvac_action(self) -> HVACAction | None:
        mode = self.grill.state.mode
        if mode is None:
            return None
        if self.grill.is_lit:
            return HVACAction.HEATING
        if mode == GrillModeType.FAN:
            return HVACAction.FAN
        return HVACAction.OFF

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self.grill.state
        return {
            "grill_mode": state.mode.name.lower() if state.mode is not None else None,
            "smoke_level": state.smoke_level,
        }

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if kwargs.get(ATTR_HVAC_MODE) == HVACMode.OFF:
            await self.async_set_hvac_mode(HVACMode.OFF)
            return
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is None:
            return
        await self.grill.async_set_run(
            int(temperature), self.grill.state.smoke_level or DEFAULT_SMOKE
        )

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == HVACMode.OFF:
            if self.grill.is_lit:
                await self.grill.async_shutdown()
            return
        if not self.grill.is_lit:
            raise HomeAssistantError(
                "Camp Chef grills must be lit at the controller; remote ignition is not supported"
            )
