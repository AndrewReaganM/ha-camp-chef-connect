"""Base entity for Camp Chef Connect."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .grill import CampChefGrill


class CampChefEntity(Entity):
    """Entity backed by a live grill connection."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, grill: CampChefGrill, key: str) -> None:
        self.grill = grill
        self._attr_unique_id = f"{grill.grill_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, grill.grill_id)},
            manufacturer="Camp Chef",
            name=grill.name,
            model=grill.model,
            sw_version=_firmware(grill),
        )

    @property
    def available(self) -> bool:
        return self.grill.available

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.grill.async_add_listener(self.async_write_ha_state))


def _firmware(grill: CampChefGrill) -> str | None:
    mcu = grill.state.stm_firmware or grill.info.get("mcuVersion")
    wifi = grill.state.esp_firmware or grill.info.get("wifiVersion")
    if mcu and wifi:
        return f"MCU {mcu} / WiFi {wifi}"
    return mcu or wifi
