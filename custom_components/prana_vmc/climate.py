"""Climate platform for Prana VMC.

Aligned to real ERP fw49 behavior:
- HVAC modes: OFF / FAN_ONLY (no HEAT selector)
- Presets in climate: manual, auto, auto_plus, night, boost (winter excluded)
- Winter/Heater are independent flags and may coexist with Auto/Auto+/Night.
  They are shown via hvac_action in the thermostat center label:
    - winter -> DEFROSTING (Sbrinamento)
    - heater -> PREHEATING (Preriscaldamento)
"""
from __future__ import annotations

import logging

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    FAN_TYPE_BOUNDED,
    FAN_TYPE_EXTRACT,
    FAN_TYPE_SUPPLY,
    SPEED_STEP,
    SWITCH_TYPE_AUTO,
    SWITCH_TYPE_AUTO_PLUS,
    SWITCH_TYPE_BOOST,
    SWITCH_TYPE_BOUND,
    SWITCH_TYPE_NIGHT,
)
from .coordinator import PranaCoordinator
from .entity import PranaEntity

_LOGGER = logging.getLogger(__name__)

FAN_MODE_OFF = "off"
FAN_MODES: list[str] = [FAN_MODE_OFF, "1", "2", "3", "4", "5", "6"]

PRESET_MANUAL = "manual"
PRESET_AUTO = "auto"
PRESET_AUTO_PLUS = "auto_plus"
PRESET_NIGHT = "night"
PRESET_BOOST = "boost"

# Winter intentionally removed (it can coexist with other modes)
PRESET_MODES: list[str] = [
    PRESET_MANUAL,
    PRESET_AUTO,
    PRESET_AUTO_PLUS,
    PRESET_NIGHT,
    PRESET_BOOST,
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Prana climate entity from a config entry."""
    coordinator: PranaCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([PranaRecuperatorClimate(coordinator, entry.entry_id)])


class PranaRecuperatorClimate(PranaEntity, ClimateEntity):
    """Climate entity representing the overall Prana ventilation (bounded fans)."""

    _attr_hvac_modes = [HVACMode.OFF, HVACMode.FAN_ONLY]
    _attr_supported_features = (
        ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_fan_modes = FAN_MODES
    _attr_preset_modes = PRESET_MODES

    # used by translations/icons.json
    _attr_has_entity_name = True
    _attr_name = None
    _attr_translation_key = "prana_climate"

    def __init__(self, coordinator: PranaCoordinator, entry_id: str) -> None:
        super().__init__(coordinator, entry_id)
        self._attr_unique_id = f"{coordinator.api.host}_ventilation"

    @property
    def temperature_unit(self) -> str:
        return self.hass.config.units.temperature_unit

    @property
    def current_temperature(self) -> float | None:
        data = self.coordinator.data
        if data is None:
            return None
        return (
            data.inside_temperature
            if data.inside_temperature is not None
            else data.inside_temperature_2
        )

    @property
    def hvac_mode(self) -> HVACMode:
        data = self.coordinator.data
        if data is None:
            return HVACMode.OFF

        any_on = (
            (data.is_fan_on(FAN_TYPE_BOUNDED) and data.bounded_speed > 0)
            or (data.is_fan_on(FAN_TYPE_SUPPLY) and data.supply_speed > 0)
            or (data.is_fan_on(FAN_TYPE_EXTRACT) and data.extract_speed > 0)
        )
        return HVACMode.FAN_ONLY if any_on else HVACMode.OFF

    @property
    def hvac_action(self) -> HVACAction | None:
        """Center label for thermostat card.

        - winter ON -> DEFROSTING (Sbrinamento)
        - heater ON -> PREHEATING (Preriscaldamento)
        - else -> FAN
        """
        data = self.coordinator.data
        if data is None:
            return None

        if self.hvac_mode == HVACMode.OFF:
            return HVACAction.OFF

        winter_on = bool(getattr(data, "winter", False))
        heater_on = bool(getattr(data, "heater", False))

        if winter_on:
            return HVACAction.DEFROSTING
        if heater_on:
            return HVACAction.PREHEATING

        return HVACAction.FAN

    @property
    def fan_mode(self) -> str | None:
        data = self.coordinator.data
        if data is None:
            return None

        # If bound mode disabled, this climate isn't the active controller
        if not getattr(data, "bound", False):
            return FAN_MODE_OFF

        if not data.is_fan_on(FAN_TYPE_BOUNDED) or data.bounded_speed <= 0:
            return FAN_MODE_OFF

        level = int(data.bounded_speed // SPEED_STEP)
        if level <= 0:
            return FAN_MODE_OFF
        return str(min(level, 6))

    @property
    def preset_mode(self) -> str | None:
        """Preset is only about boost/night/auto+/auto/manual. Winter excluded."""
        data = self.coordinator.data
        if data is None:
            return None

        if getattr(data, "boost", False):
            return PRESET_BOOST
        if getattr(data, "night", False):
            return PRESET_NIGHT
        if getattr(data, "auto_plus", False):
            return PRESET_AUTO_PLUS
        if getattr(data, "auto", False):
            return PRESET_AUTO
        return PRESET_MANUAL

    async def _ensure_bound_mode(self) -> None:
        data = self.coordinator.data
        if data is not None and getattr(data, "bound", False):
            return
        await self.coordinator.async_set_switch(SWITCH_TYPE_BOUND, True)

    async def _set_bounded_speed_level(self, level: int) -> None:
        level = max(0, min(6, int(level)))

        data = self.coordinator.data
        is_on = data.is_fan_on(FAN_TYPE_BOUNDED) if data else False

        if level == 0:
            if is_on:
                await self.coordinator.async_set_fan_on(False, FAN_TYPE_BOUNDED)
            return

        api_speed = level * SPEED_STEP
        await self.coordinator.async_set_speed(api_speed, FAN_TYPE_BOUNDED)
        if not is_on:
            await self.coordinator.async_set_fan_on(True, FAN_TYPE_BOUNDED)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == HVACMode.OFF:
            await self.coordinator.async_power_off()
            return

        if hvac_mode == HVACMode.FAN_ONLY:
            await self._ensure_bound_mode()

            level = 1
            data = self.coordinator.data
            if data and data.bounded_speed > 0:
                level = max(1, min(6, int(data.bounded_speed // SPEED_STEP)))

            await self._set_bounded_speed_level(level)
            return

        raise ValueError(f"Unsupported HVAC mode: {hvac_mode}")

    async def async_turn_on(self) -> None:
        await self.async_set_hvac_mode(HVACMode.FAN_ONLY)

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        await self._ensure_bound_mode()

        if fan_mode == FAN_MODE_OFF:
            await self._set_bounded_speed_level(0)
            return

        level = int(fan_mode)
        await self._set_bounded_speed_level(level)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        if preset_mode not in PRESET_MODES:
            raise ValueError(f"Unsupported preset mode: {preset_mode}")

        if self.hvac_mode == HVACMode.OFF:
            await self.async_set_hvac_mode(HVACMode.FAN_ONLY)

        if preset_mode == PRESET_MANUAL:
            # IMPORTANT: do NOT touch winter/heater here (they may be enabled automatically)
            for sw in (
                SWITCH_TYPE_BOOST,
                SWITCH_TYPE_NIGHT,
                SWITCH_TYPE_AUTO_PLUS,
                SWITCH_TYPE_AUTO,
            ):
                await self.coordinator.async_set_switch(sw, False)
            return

        if preset_mode == PRESET_AUTO:
            await self.coordinator.async_set_switch(SWITCH_TYPE_AUTO, True)
            return

        if preset_mode == PRESET_AUTO_PLUS:
            await self.coordinator.async_set_switch(SWITCH_TYPE_AUTO_PLUS, True)
            return

        if preset_mode == PRESET_NIGHT:
            await self.coordinator.async_set_switch(SWITCH_TYPE_NIGHT, True)
            return

        if preset_mode == PRESET_BOOST:
            await self.coordinator.async_set_switch(SWITCH_TYPE_BOOST, True)
            return