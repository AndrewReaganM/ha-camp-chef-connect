"""Smoke level control."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CampChefConfigEntry
from .const import SMOKE_MAX, SMOKE_MIN
from .entity import CampChefEntity
from .grill import CampChefGrill
from .protocol import GrillModeType


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CampChefConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities(CampChefSmokeLevel(grill) for grill in entry.runtime_data.grills)


class CampChefSmokeLevel(CampChefEntity, NumberEntity):
    """Smoke level; only meaningful while the grill is in RUN mode."""

    _attr_translation_key = "smoke_level"
    _attr_native_min_value = SMOKE_MIN
    _attr_native_max_value = SMOKE_MAX
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER
    _attr_icon = "mdi:smoke"

    def __init__(self, grill: CampChefGrill) -> None:
        super().__init__(grill, "smoke_level")

    @property
    def available(self) -> bool:
        return super().available and self.grill.state.mode == GrillModeType.RUN

    @property
    def native_value(self) -> float | None:
        return self.grill.state.smoke_level

    async def async_set_native_value(self, value: float) -> None:
        if (set_temp := self.grill.state.set_temp_f) is None:
            raise HomeAssistantError("The grill has not reported its set temperature yet")
        await self.grill.async_set_run(set_temp, int(value), await self._requested_by())
