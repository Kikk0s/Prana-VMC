"""API Client for Prana VMC devices."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from .const import (
    DEFAULT_PORT,
    DEFAULT_TIMEOUT,
    FAN_TYPE_BOUNDED,
    FAN_TYPE_EXTRACT,
    FAN_TYPE_SUPPLY,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class PranaState:
    """Represents the state of a Prana device."""

    # Fan states
    extract_speed: int = 0
    extract_is_on: bool = False
    extract_max_speed: int = 60

    supply_speed: int = 0
    supply_is_on: bool = False
    supply_max_speed: int = 60

    bounded_speed: int = 0
    bounded_is_on: bool = False
    bounded_max_speed: int = 60

    # Modes
    bound: bool = True
    heater: bool = False
    auto: bool = False
    auto_plus: bool = False
    winter: bool = False
    night: bool = False
    boost: bool = False

    # Display
    brightness: int = 6

    # Sensors (optional)
    inside_temperature: float | None = None
    inside_temperature_2: float | None = None
    outside_temperature: float | None = None
    outside_temperature_2: float | None = None
    humidity: float | None = None
    co2: int | None = None
    voc: int | None = None
    air_pressure: float | None = None

    # Raw data
    raw_data: dict = field(default_factory=dict)

    @classmethod
    def from_api_response(cls, data: dict) -> PranaState:
        """Create PranaState from API response."""
        extract_data = data.get("extract", {})
        supply_data = data.get("supply", {})
        bounded_data = data.get("bounded", {})

        # Helper function to convert temperature (API returns 145 for 14.5°C)
        def parse_temperature(value: int | float | None) -> float | None:
            if value is None:
                return None
            return value / 10.0

        def parse_bool(value: Any) -> bool:
            """Parse bool-ish values from API (0/1, '0'/'1', true/false)."""
            if value is None:
                return False
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return bool(int(value))
            if isinstance(value, str):
                v = value.strip().lower()
                if v in ("1", "true", "on", "yes"):
                    return True
                if v in ("0", "false", "off", "no", ""):
                    return False
            return bool(value)

        def normalize_speed(value: Any) -> int:
            """Normalize fan speed to the 0..60 scale (step 10).

            Some firmwares report speed as a level 0..6 instead of 0..60.
            Internally we store 0..60 so entities can consistently map to 0..6.
            """
            try:
                s = int(float(value))  # handles '3', 3, 3.0
            except (TypeError, ValueError):
                return 0

            if s < 0:
                return 0

            # Firmware variant: 0..6 level
            if 0 <= s <= 6:
                return s * 10

            # Standard variant: 0..60 step 10
            if 0 <= s <= 60 and s % 10 == 0:
                return s

            # Odd-but-seen variants: 1..9 meaning 10..90 (clamp)
            if 0 < s < 10:
                return min(s * 10, 60)

            return min(s, 60)

        def normalize_max_speed(value: Any) -> int:
            s = normalize_speed(value)
            return 60 if s == 0 else s


        # Night mode rules (matches the behavior you described):
        # - If AUTO or AUTO+ are active -> NIGHT is always considered OFF
        # - If AUTO/AUTO+ are OFF and both fans are at level 1 -> NIGHT is ON
        auto = parse_bool(data.get("auto", False))
        auto_plus = parse_bool(data.get("auto_plus", False))
        bound = parse_bool(data.get("bound", True))
        supply_speed_raw = normalize_speed(supply_data.get("speed", 0))
        extract_speed_raw = normalize_speed(extract_data.get("speed", 0))
        bounded_speed_raw = normalize_speed(bounded_data.get("speed", 0))
        night_effective = False
        if not (auto or auto_plus):
            if supply_speed_raw == 10 and extract_speed_raw == 10:
                night_effective = True
            elif bound and bounded_speed_raw == 10:
                night_effective = True

        return cls(
            extract_speed=normalize_speed(extract_data.get("speed", 0)),
            extract_is_on=parse_bool(extract_data.get("is_on", False)),
            extract_max_speed=normalize_max_speed(extract_data.get("max_speed", 60)),
            supply_speed=normalize_speed(supply_data.get("speed", 0)),
            supply_is_on=parse_bool(supply_data.get("is_on", False)),
            supply_max_speed=normalize_max_speed(supply_data.get("max_speed", 60)),
            bounded_speed=normalize_speed(bounded_data.get("speed", 0)),
            bounded_is_on=parse_bool(bounded_data.get("is_on", False)),
            bounded_max_speed=normalize_max_speed(bounded_data.get("max_speed", 60)),
            bound=bound,
            heater=parse_bool(data.get("heater", False)),
            auto=auto,
            auto_plus=auto_plus,
            winter=parse_bool(data.get("winter", False)),
            night=night_effective,
            boost=parse_bool(data.get("boost", False)),
            brightness=int(data.get("brightness", 6) or 6),
            inside_temperature=parse_temperature(data.get("inside_temperature")),
            inside_temperature_2=parse_temperature(data.get("inside_temperature_2")),
            outside_temperature=parse_temperature(data.get("outside_temperature")),
            outside_temperature_2=parse_temperature(data.get("outside_temperature_2")),
            humidity=data.get("humidity"),
            co2=data.get("co2"),
            voc=data.get("voc"),
            air_pressure=data.get("air_pressure"),
            raw_data=data,
        )

    def get_speed_percentage(self, fan_type: str) -> int:
        """Get speed as percentage (0-100)."""
        if fan_type == FAN_TYPE_EXTRACT:
            speed = self.extract_speed
            max_speed = self.extract_max_speed
        elif fan_type == FAN_TYPE_SUPPLY:
            speed = self.supply_speed
            max_speed = self.supply_max_speed
        else:  # bounded
            speed = self.bounded_speed
            max_speed = self.bounded_max_speed

        if max_speed == 0:
            return 0
        return int((speed / max_speed) * 100)

    def is_fan_on(self, fan_type: str) -> bool:
        """Check if a fan type is on."""
        if fan_type == FAN_TYPE_EXTRACT:
            return self.extract_is_on
        elif fan_type == FAN_TYPE_SUPPLY:
            return self.supply_is_on
        return self.bounded_is_on


class PranaApiError(Exception):
    """Base exception for Prana API errors."""


class PranaConnectionError(PranaApiError):
    """Connection error."""


class PranaApiClient:
    """API Client for Prana VMC devices."""

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_PORT,
        timeout: int = DEFAULT_TIMEOUT,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        """Initialize the API client."""
        self._host = host
        self._port = port
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session = session
        self._own_session = session is None
        self._base_url = f"http://{host}:{port}"

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self._session is None or self._session.closed:
            # Some Prana firmwares behave better if we don't reuse keep-alive connections.
            connector = aiohttp.TCPConnector(force_close=True, enable_cleanup_closed=True)
            self._session = aiohttp.ClientSession(timeout=self._timeout, connector=connector)
            self._own_session = True
        return self._session

    async def close(self) -> None:
        """Close the session if we own it."""
        if self._own_session and self._session and not self._session.closed:
            await self._session.close()

    async def _request(
        self,
        method: str,
        endpoint: str,
        data: dict | None = None,
    ) -> dict:
        """Make an API request."""
        session = await self._get_session()
        url = f"{self._base_url}/{endpoint}"

        _LOGGER.debug(
            "API Request: %s %s data=%s",
            method,
            url,
            data,
        )

        headers = {
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Connection": "close",
        }
        params = {"_": str(int(time.time() * 1000))}

        try:
            if method == "GET":
                async with session.get(url, params=params, headers=headers) as response:
                    response.raise_for_status()
                    result = await response.json()
                    _LOGGER.debug("API Response: %s", result)
                    return result
            else:  # POST
                async with session.post(url, json=data, params=params, headers=headers) as response:
                    response.raise_for_status()
                    # POST endpoints may return empty response
                    try:
                        result = await response.json()
                        _LOGGER.debug("API Response: %s", result)
                        return result
                    except aiohttp.ContentTypeError:
                        _LOGGER.debug("API Response: empty (no content)")
                        return {}
        except aiohttp.ClientConnectorError as err:
            _LOGGER.error("Connection error to %s: %s", self._host, err)
            raise PranaConnectionError(f"Cannot connect to {self._host}: {err}") from err
        except aiohttp.ClientResponseError as err:
            _LOGGER.error("HTTP error from %s: %s %s", self._host, err.status, err.message)
            raise PranaApiError(f"HTTP error {err.status}: {err.message}") from err
        except aiohttp.ClientError as err:
            _LOGGER.error("API request failed for %s: %s", self._host, err)
            raise PranaApiError(f"API request failed: {err}") from err
        except asyncio.TimeoutError as err:
            _LOGGER.error("Timeout connecting to %s", self._host)
            raise PranaConnectionError(f"Timeout connecting to {self._host}") from err

    async def get_state(self) -> PranaState:
        """Get the current device state."""
        data = await self._request("GET", "getState")
        return PranaState.from_api_response(data)

    async def set_speed(self, speed: int, fan_type: str) -> None:
        """Set fan speed.

        Args:
            speed: Speed value (must be multiple of 10, e.g., 10, 20, ..., 60)
            fan_type: Fan type ('supply', 'extract', or 'bounded')
        """
        if speed % 10 != 0 or speed < 0 or speed > 60:
            raise ValueError("Speed must be 0 or a multiple of 10 between 10 and 60")
        if fan_type not in (FAN_TYPE_SUPPLY, FAN_TYPE_EXTRACT, FAN_TYPE_BOUNDED):
            raise ValueError(f"Invalid fan type: {fan_type}")

        await self._request(
            "POST",
            "setSpeed",
            {"speed": speed, "fanType": fan_type},
        )

    async def set_speed_is_on(self, value: bool, fan_type: str) -> None:
        """Turn fan on or off.

        Args:
            value: True to turn on, False to turn off
            fan_type: Fan type ('supply', 'extract', or 'bounded')
        """
        if fan_type not in (FAN_TYPE_SUPPLY, FAN_TYPE_EXTRACT, FAN_TYPE_BOUNDED):
            raise ValueError(f"Invalid fan type: {fan_type}")

        await self._request(
            "POST",
            "setSpeedIsOn",
            {"value": value, "fanType": fan_type},
        )

    async def set_switch(self, switch_type: str, value: bool) -> None:
        """Enable or disable a mode.

        Args:
            switch_type: Mode type ('bound', 'heater', 'night', 'boost', 'auto', 'auto_plus', 'winter')
            value: True to enable, False to disable
        """
        valid_switches = ("bound", "heater", "night", "boost", "auto", "auto_plus", "winter")
        if switch_type not in valid_switches:
            raise ValueError(f"Invalid switch type: {switch_type}")

        await self._request(
            "POST",
            "setSwitch",
            {"switchType": switch_type, "value": value},
        )

    async def set_brightness(self, brightness: int) -> None:
        """Set display brightness.

        Args:
            brightness: Brightness value (0, 1, 2, 4, 8, 16, or 32)
        """
        valid_brightness = (0, 1, 2, 4, 8, 16, 32)
        if brightness not in valid_brightness:
            raise ValueError(f"Invalid brightness value: {brightness}")

        await self._request(
            "POST",
            "setBrightness",
            {"brightness": brightness},
        )

    async def test_connection(self) -> dict:
        """Test the connection to the device."""
        return await self._request("GET", "getState")

    @property
    def host(self) -> str:
        """Return the host."""
        return self._host