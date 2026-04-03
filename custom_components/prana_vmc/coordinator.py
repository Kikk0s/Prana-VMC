"""DataUpdateCoordinator for Prana VMC."""
from __future__ import annotations

import asyncio
import time
from datetime import timedelta
import logging
from typing import Any
from dataclasses import replace

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import PranaApiClient, PranaApiError, PranaConnectionError, PranaState
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Polling interval - reduced to 15 seconds for better sync with external app
SCAN_INTERVAL = timedelta(seconds=15)

# Delay after making a change before refreshing state (let device process)
POST_COMMAND_DELAY = 2.0

# Keep UI stable right after a command, even if the device reports stale state for a few seconds
PENDING_WINDOW = 6.0

# Maximum retries for commands
MAX_RETRIES = 3
RETRY_DELAY = 1.0


class PranaCoordinator(DataUpdateCoordinator[PranaState]):
    """Prana data update coordinator."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        api: PranaApiClient,
        device_name: str,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} ({device_name})",
            update_interval=SCAN_INTERVAL,
        )
        self.api = api
        self.device_name = device_name
        # Lock to prevent concurrent modifications
        self._command_lock = asyncio.Lock()

        # Pending optimistic changes (workaround for some firmwares that report stale /getState for a short time)
        self._pending_patch: dict[str, Any] = {}
        self._pending_until: float = 0.0

        # Remember last speeds so we can restore when disabling Night/Boost
        self._saved_speed_state: dict[str, Any] = {}
        self._last_set_brightness: int | None = None


    def _set_pending(self, patch: dict[str, Any]) -> None:
        """Store an optimistic patch for a short window and push it to listeners immediately."""
        if not patch:
            return

        # Merge patches (multiple rapid commands)
        self._pending_patch.update(patch)
        self._pending_until = time.monotonic() + PENDING_WINDOW

        # Push to listeners now (keeps UI from snapping back)
        if self.data is not None:
            try:
                updated = replace(self.data, **patch)
            except TypeError:
                # In case HA changes coordinator typing, fallback
                updated = self.data
                for k, v in patch.items():
                    setattr(updated, k, v)
            # DataUpdateCoordinator provides this helper in modern HA
            if hasattr(self, "async_set_updated_data"):
                self.async_set_updated_data(updated)  # type: ignore[attr-defined]
            else:
                self.data = updated
                self.async_update_listeners()


    def _apply_pending_to_fetched(self, state: PranaState) -> PranaState:
        """If we have a pending patch and the device still reports old state, keep UI stable."""
        # Apply pending patch if active
        if self._pending_patch and time.monotonic() < self._pending_until:
            # If device now matches, clear pending
            all_match = True
            for k, v in self._pending_patch.items():
                if getattr(state, k, None) != v:
                    all_match = False
                    break
    
            if all_match:
                self._pending_patch.clear()
            else:
                try:
                    state = replace(state, **self._pending_patch)
                except TypeError:
                    for k, v in self._pending_patch.items():
                        setattr(state, k, v)
        else:
            # Expired
            self._pending_patch.clear()
    
        # Brightness "sticky" workaround:
        # Some devices always report brightness as 0 in /getState even when it's not.
        if self._last_set_brightness is not None:
            fetched_brightness = getattr(state, "brightness", None)
            if fetched_brightness not in (None, 0):
                # Device reports a real value -> trust it
                self._last_set_brightness = int(fetched_brightness)
            else:
                # Treat 0 as unreliable only while the unit is running
                any_fan_on = bool(
                    getattr(state, "bounded_is_on", False)
                    or getattr(state, "supply_is_on", False)
                    or getattr(state, "extract_is_on", False)
                )
                if any_fan_on and self._last_set_brightness != 0:
                    try:
                        state = replace(state, brightness=self._last_set_brightness)
                    except TypeError:
                        setattr(state, "brightness", self._last_set_brightness)
    
        return state

    def _save_current_speeds(self, current_state: PranaState) -> None:
        """Save current speeds/on-states so we can restore later."""
        self._saved_speed_state = {
            "bound": current_state.bound,
            "supply_speed": current_state.supply_speed,
            "supply_is_on": current_state.supply_is_on,
            "extract_speed": current_state.extract_speed,
            "extract_is_on": current_state.extract_is_on,
            "bounded_speed": current_state.bounded_speed,
            "bounded_is_on": current_state.bounded_is_on,
        }

    async def _restore_saved_speeds(self) -> None:
        """Restore speeds saved by _save_current_speeds (best effort)."""
        if not self._saved_speed_state:
            return

        bound = bool(self._saved_speed_state.get("bound", True))
        if bound:
            speed = int(self._saved_speed_state.get("bounded_speed", 20) or 20)
            is_on = bool(self._saved_speed_state.get("bounded_is_on", True))
            await self._execute_command_with_retry(self.api.set_speed, speed, "bounded")
            await self._execute_command_with_retry(self.api.set_speed_is_on, is_on, "bounded")
        else:
            for fan_type in ("supply", "extract"):
                speed = int(self._saved_speed_state.get(f"{fan_type}_speed", 20) or 20)
                is_on = bool(self._saved_speed_state.get(f"{fan_type}_is_on", True))
                await self._execute_command_with_retry(self.api.set_speed, speed, fan_type)
                await self._execute_command_with_retry(self.api.set_speed_is_on, is_on, fan_type)

    async def _async_update_data(self) -> PranaState:
        """Fetch data from API."""
        try:
            state = await self.api.get_state()
            return self._apply_pending_to_fetched(state)
        except PranaConnectionError as err:
            raise UpdateFailed(f"Error communicating with device: {err}") from err
        except PranaApiError as err:
            raise UpdateFailed(f"Error fetching data: {err}") from err

    async def _execute_command_with_retry(
        self,
        command_func,
        *args,
        **kwargs,
    ) -> None:
        """Execute a command with retry logic."""
        last_error = None
        
        for attempt in range(MAX_RETRIES):
            try:
                await command_func(*args, **kwargs)
                return
            except PranaApiError as err:
                last_error = err
                _LOGGER.warning(
                    "Command failed (attempt %d/%d): %s",
                    attempt + 1,
                    MAX_RETRIES,
                    err,
                )
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY)
        
        raise last_error

    async def _refresh_after_command(self) -> None:
        """Refresh data after a command with a small delay.

        Some Prana firmwares can report stale /getState for ~1-3 seconds after a change.
        We refresh a few times and keep optimistic values for PENDING_WINDOW seconds.
        """
        for delay in (POST_COMMAND_DELAY, 1.0, 2.0):
            await asyncio.sleep(delay)
            await self.async_refresh()
            # If pending patch is cleared (device confirmed), stop early
            if not self._pending_patch:
                return


    async def async_power_off(self) -> None:
        """Turn the unit off (best effort).

        Some modes (AUTO/AUTO+/NIGHT/BOOST/WINTER/HEATER) can keep fans running.
        We disable them and then turn off all fans.
        """
        async with self._command_lock:
            try:
                current_state = await self.api.get_state()

                # Optimistic UI update (prevents the card from snapping back)
                patch: dict[str, Any] = {
                    "auto": False,
                    "auto_plus": False,
                    "night": False,
                    "boost": False,
                    "winter": False,
                    "heater": False,
                    "supply_speed": 0,
                    "supply_is_on": False,
                    "extract_speed": 0,
                    "extract_is_on": False,
                    "bounded_speed": 0,
                    "bounded_is_on": False,
                }
                self._set_pending(patch)

                # Disable modes first
                for sw in ("auto", "auto_plus", "night", "boost", "winter", "heater"):
                    if getattr(current_state, sw, False):
                        await self._execute_command_with_retry(self.api.set_switch, sw, False)

                # Turn off all fans (bounded + separate fans)
                for ft in ("bounded", "supply", "extract"):
                    await self._execute_command_with_retry(self.api.set_speed_is_on, False, ft)
                    await self._execute_command_with_retry(self.api.set_speed, 0, ft)

                await self._refresh_after_command()

            except PranaApiError as err:
                _LOGGER.error("Failed to power off: %s", err)
                self._pending_patch.clear()
                await self.async_refresh()
                raise

    async def async_set_speed(self, speed: int, fan_type: str) -> None:
        """Set fan speed and refresh data."""
        async with self._command_lock:
            try:
                current_state = await self.api.get_state()
                _LOGGER.debug(
                    "Setting %s speed to %d (current: %s)",
                    fan_type,
                    speed,
                    current_state.raw_data,
                )

                patch: dict[str, Any] = {}
                if fan_type == "supply":
                    patch = {"supply_speed": speed, "supply_is_on": speed > 0}
                elif fan_type == "extract":
                    patch = {"extract_speed": speed, "extract_is_on": speed > 0}
                else:
                    patch = {"bounded_speed": speed, "bounded_is_on": speed > 0}

                # Optimistic UI update (prevents snapping back)
                self._set_pending(patch)

                await self._execute_command_with_retry(self.api.set_speed, speed, fan_type)

                # Some firmwares require an explicit "on" when changing speed from 0
                if speed > 0 and not current_state.is_fan_on(fan_type):
                    await self._execute_command_with_retry(self.api.set_speed_is_on, True, fan_type)
                    # Keep patch consistent
                    if fan_type == "supply":
                        self._set_pending({"supply_is_on": True})
                    elif fan_type == "extract":
                        self._set_pending({"extract_is_on": True})
                    else:
                        self._set_pending({"bounded_is_on": True})

                await self._refresh_after_command()

            except PranaApiError as err:
                _LOGGER.error("Failed to set speed: %s", err)
                self._pending_patch.clear()
                await self.async_refresh()
                raise

    async def async_set_fan_on(self, value: bool, fan_type: str) -> None:
        """Turn fan on/off and refresh data."""
        async with self._command_lock:
            try:
                current_state = await self.api.get_state()
                _LOGGER.debug(
                    "Setting %s fan to %s (current is_on: %s)",
                    fan_type,
                    value,
                    current_state.is_fan_on(fan_type),
                )

                patch: dict[str, Any] = {}
                if fan_type == "supply":
                    patch = {"supply_is_on": value}
                    if not value:
                        patch["supply_speed"] = 0
                elif fan_type == "extract":
                    patch = {"extract_is_on": value}
                    if not value:
                        patch["extract_speed"] = 0
                else:
                    patch = {"bounded_is_on": value}
                    if not value:
                        patch["bounded_speed"] = 0

                self._set_pending(patch)

                await self._execute_command_with_retry(self.api.set_speed_is_on, value, fan_type)

                # If turning off, many firmwares also set speed to 0 (keep HA consistent)
                if not value:
                    await self._execute_command_with_retry(self.api.set_speed, 0, fan_type)

                await self._refresh_after_command()

            except PranaApiError as err:
                _LOGGER.error("Failed to set fan state: %s", err)
                self._pending_patch.clear()
                await self.async_refresh()
                raise

    async def async_set_switch(self, switch_type: str, value: bool) -> None:
        """Set switch state and refresh data.

        Implements extra behavior to match Prana app logic:
        - AUTO / AUTO+ cannot coexist with NIGHT
        - NIGHT means both fans at level 1 with AUTO/AUTO+ off
        - BOOST sets speed to max (6) and disables AUTO/AUTO+/NIGHT
        - When disabling NIGHT/BOOST we restore previous speeds (best effort)
        """
        async with self._command_lock:
            try:
                current_state = await self.api.get_state()
                _LOGGER.debug(
                    "Setting switch %s to %s (current state: %s)",
                    switch_type,
                    value,
                    getattr(current_state, switch_type, None),
                )

                patch: dict[str, Any] = {}
                commands: list[tuple] = []

                # Helpers
                def disable_modes_if_needed(modes: list[str]) -> None:
                    for m in modes:
                        if getattr(current_state, m, False):
                            commands.append((self.api.set_switch, m, False))
                            patch[m] = False

                # 1) AUTO / AUTO+ ON => force NIGHT OFF
                if switch_type in ("auto", "auto_plus") and value:
                    # If NIGHT/BOOST were active, disable them
                    if getattr(current_state, "night", False):
                        commands.append((self.api.set_switch, "night", False))
                        patch["night"] = False
                    if getattr(current_state, "boost", False):
                        commands.append((self.api.set_switch, "boost", False))
                        patch["boost"] = False

                    commands.append((self.api.set_switch, switch_type, True))
                    patch[switch_type] = True

                # 2) NIGHT mode
                elif switch_type == "night":
                    if value:
                        # Save current speeds so we can restore when NIGHT is disabled
                        self._save_current_speeds(current_state)

                        # Disable AUTO/AUTO+/BOOST first
                        disable_modes_if_needed(["auto", "auto_plus", "boost"])

                        # Set fans to level 1
                        if current_state.bound:
                            commands.append((self.api.set_speed, 10, "bounded"))
                            commands.append((self.api.set_speed_is_on, True, "bounded"))
                            patch.update({"bounded_speed": 10, "bounded_is_on": True})
                        else:
                            for ft in ("supply", "extract"):
                                commands.append((self.api.set_speed, 10, ft))
                                commands.append((self.api.set_speed_is_on, True, ft))
                            patch.update(
                                {
                                    "supply_speed": 10,
                                    "supply_is_on": True,
                                    "extract_speed": 10,
                                    "extract_is_on": True,
                                }
                            )

                        # Some firmwares also have a night switch; set it as well
                        commands.append((self.api.set_switch, "night", True))
                        patch["night"] = True
                    else:
                        # Disable the switch and restore previous speeds (so NIGHT no longer matches)
                        commands.append((self.api.set_switch, "night", False))
                        patch["night"] = False

                        # Restore previous speeds (best effort)
                        await self._restore_saved_speeds()

                # 3) BOOST mode
                elif switch_type == "boost":
                    if value:
                        self._save_current_speeds(current_state)

                        # Disable AUTO/AUTO+/NIGHT first
                        disable_modes_if_needed(["auto", "auto_plus", "night"])

                        # Max speed (6)
                        if current_state.bound:
                            commands.append((self.api.set_speed, 60, "bounded"))
                            commands.append((self.api.set_speed_is_on, True, "bounded"))
                            patch.update({"bounded_speed": 60, "bounded_is_on": True})
                        else:
                            for ft in ("supply", "extract"):
                                commands.append((self.api.set_speed, 60, ft))
                                commands.append((self.api.set_speed_is_on, True, ft))
                            patch.update(
                                {
                                    "supply_speed": 60,
                                    "supply_is_on": True,
                                    "extract_speed": 60,
                                    "extract_is_on": True,
                                }
                            )

                        commands.append((self.api.set_switch, "boost", True))
                        patch["boost"] = True
                    else:
                        commands.append((self.api.set_switch, "boost", False))
                        patch["boost"] = False
                        await self._restore_saved_speeds()

                # 4) Other switches: heater / winter / bound / etc.
                else:
                    commands.append((self.api.set_switch, switch_type, value))
                    patch[switch_type] = value

                # Push optimistic patch immediately
                self._set_pending(patch)

                # Execute all commands
                for cmd in commands:
                    func, *args = cmd
                    await self._execute_command_with_retry(func, *args)

                await self._refresh_after_command()

            except PranaApiError as err:
                _LOGGER.error("Failed to set switch: %s", err)
                self._pending_patch.clear()
                await self.async_refresh()
                raise

    async def async_set_brightness(self, brightness: int) -> None:
        """Set brightness and refresh data."""
        async with self._command_lock:
            try:
                current_state = await self.api.get_state()
                _LOGGER.debug(
                    "Setting brightness to %d (current: %d)",
                    brightness,
                    current_state.brightness,
                )

                self._last_set_brightness = brightness

                self._set_pending({"brightness": brightness})

                await self._execute_command_with_retry(self.api.set_brightness, brightness)

                await self._refresh_after_command()

            except PranaApiError as err:
                _LOGGER.error("Failed to set brightness: %s", err)
                self._pending_patch.clear()
                await self.async_refresh()
                raise

    async def async_force_refresh(self) -> None:
        """Force an immediate refresh of the data."""
        async with self._command_lock:
            await self.async_refresh()
