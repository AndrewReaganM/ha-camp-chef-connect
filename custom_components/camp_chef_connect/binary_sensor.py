"""Binary sensors for Camp Chef Connect."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CampChefConfigEntry
from .entity import CampChefEntity
from .grill import CampChefGrill
from .protocol import FaultCode


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CampChefConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    grills = entry.runtime_data.grills
    async_add_entities(
        [CampChefConnected(g) for g in grills]
        + [CampChefFault(g) for g in grills]
        + [CampChefTransitioning(g) for g in grills]
    )


class CampChefConnected(CampChefEntity, BinarySensorEntity):
    """Whether the grill is reachable through the cloud right now."""

    _attr_translation_key = "connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, grill: CampChefGrill) -> None:
        super().__init__(grill, "connected")

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool:
        return self.grill.available


class CampChefFault(CampChefEntity, BinarySensorEntity):
    """On when the controller reports a fault (flame out, over temp, sensor)."""

    _attr_translation_key = "fault"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, grill: CampChefGrill) -> None:
        super().__init__(grill, "fault")

    @property
    def is_on(self) -> bool | None:
        fault = self.grill.state.fault
        return None if fault is None else fault != FaultCode.NONE


class CampChefTransitioning(CampChefEntity, BinarySensorEntity):
    """On while the controller is moving between modes (startup, shutdown)."""

    _attr_translation_key = "transitioning"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, grill: CampChefGrill) -> None:
        super().__init__(grill, "transitioning")

    @property
    def is_on(self) -> bool | None:
        return self.grill.state.transitioning
