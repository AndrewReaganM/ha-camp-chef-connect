"""Sensors for Camp Chef Connect."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CampChefConfigEntry
from .entity import CampChefEntity
from .grill import CampChefGrill
from .protocol import FaultCode, GrillModeType, GrillState


@dataclass(frozen=True, kw_only=True)
class CampChefSensorDescription(SensorEntityDescription):
    value_fn: Callable[[GrillState], str | int | None]


def _mode(state: GrillState) -> str | None:
    return state.mode.name.lower() if state.mode is not None else None


def _fault(state: GrillState) -> str | None:
    return state.fault.name.lower() if state.fault is not None else None


SENSORS: tuple[CampChefSensorDescription, ...] = (
    CampChefSensorDescription(
        key="chamber_temperature",
        translation_key="chamber_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
        value_fn=lambda s: s.chamber_f,
    ),
    CampChefSensorDescription(
        key="set_temperature",
        translation_key="set_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
        value_fn=lambda s: s.set_temp_f,
    ),
    CampChefSensorDescription(
        key="pellet_level",
        translation_key="pellet_level",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:grain",
        value_fn=lambda s: s.pellet_level,
    ),
    CampChefSensorDescription(
        key="mode",
        translation_key="mode",
        device_class=SensorDeviceClass.ENUM,
        options=[m.name.lower() for m in GrillModeType],
        value_fn=_mode,
    ),
    CampChefSensorDescription(
        key="last_fault",
        translation_key="last_fault",
        device_class=SensorDeviceClass.ENUM,
        options=[f.name.lower() for f in FaultCode],
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_fault,
    ),
    CampChefSensorDescription(
        key="wifi_rssi",
        translation_key="wifi_rssi",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda s: s.wifi_rssi,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CampChefConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    entities: list[SensorEntity] = []
    for grill in entry.runtime_data.grills:
        entities += [CampChefSensor(grill, d) for d in SENSORS]
        entities += [CampChefProbe(grill, i) for i in range(grill.state.probe_count)]
    async_add_entities(entities)


class CampChefSensor(CampChefEntity, SensorEntity):
    """Sensor reading one field of the grill state."""

    entity_description: CampChefSensorDescription

    def __init__(self, grill: CampChefGrill, description: CampChefSensorDescription) -> None:
        super().__init__(grill, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> str | int | None:
        return self.entity_description.value_fn(self.grill.state)


class CampChefProbe(CampChefEntity, SensorEntity):
    """Meat probe temperature; unknown when nothing is plugged in."""

    _attr_translation_key = "probe"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.FAHRENHEIT

    def __init__(self, grill: CampChefGrill, index: int) -> None:
        super().__init__(grill, f"probe_{index + 1}")
        self._index = index
        self._attr_translation_placeholders = {"number": str(index + 1)}

    @property
    def native_value(self) -> int | None:
        return self.grill.state.probes_f.get(self._index)
