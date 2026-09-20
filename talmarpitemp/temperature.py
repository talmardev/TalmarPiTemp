"""CPU temperature source and unit conversion."""

from __future__ import annotations

import math
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional

MIN_PLAUSIBLE_C = -50.0
MAX_PLAUSIBLE_C = 150.0

THERMAL_ROOT = Path("/sys/class/thermal")
PRIMARY_ZONE = "thermal_zone0"

UNIT_CELSIUS = "C"
UNIT_FAHRENHEIT = "F"
UNIT_NAMES = {UNIT_CELSIUS: "Celsius", UNIT_FAHRENHEIT: "Fahrenheit"}
_UNIT_ALIASES = {"c": UNIT_CELSIUS, "celsius": UNIT_CELSIUS,
                 "f": UNIT_FAHRENHEIT, "fahrenheit": UNIT_FAHRENHEIT}

_VCGENCMD_PATTERN = re.compile(r"temp=(-?\d+(?:\.\d+)?)")


class TemperatureReadError(Exception):
    """The temperature source is missing or returned unusable data."""


def celsius_to_fahrenheit(celsius: float) -> float:
    return celsius * 9 / 5 + 32


def fahrenheit_to_celsius(fahrenheit: float) -> float:
    return (fahrenheit - 32) * 5 / 9


def normalize_unit(text: str) -> str:
    """Return "C" or "F" for user input such as "f" or "Celsius"."""
    key = str(text).strip().lower()
    if key not in _UNIT_ALIASES:
        raise ValueError("Unit must be C or F (Celsius or Fahrenheit)")
    return _UNIT_ALIASES[key]


def from_celsius(celsius: float, unit: str) -> float:
    """Convert a stored Celsius value to the display unit."""
    return celsius_to_fahrenheit(celsius) if unit == UNIT_FAHRENHEIT else celsius


def unit_symbol(unit: str) -> str:
    return "°" + unit


def is_plausible(celsius: float) -> bool:
    return math.isfinite(celsius) and MIN_PLAUSIBLE_C <= celsius <= MAX_PLAUSIBLE_C


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="ascii", errors="replace")
    except OSError:
        return ""


class SysfsSensor:
    """Reads a thermal file that holds millidegrees Celsius."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @property
    def description(self) -> str:
        return str(self.path)

    def read_celsius(self) -> float:
        try:
            raw = self.path.read_text(encoding="ascii", errors="replace")
        except OSError as exc:
            raise TemperatureReadError(
                f"Cannot read {self.path}: {exc.strerror or exc}") from exc
        try:
            value = float(raw.strip())
        except ValueError as exc:
            raise TemperatureReadError(
                f"Unexpected content in {self.path}: {raw.strip()[:40]!r}") from exc
        return value / 1000.0 if abs(value) >= 1000 else value


class VcgencmdSensor:
    """Fallback for systems without a readable thermal zone."""

    description = "vcgencmd measure_temp"

    def __init__(self, executable: str, run: Callable = subprocess.run) -> None:
        self._executable = executable
        self._run = run

    def read_celsius(self) -> float:
        try:
            result = self._run([self._executable, "measure_temp"], capture_output=True,
                               text=True, timeout=3, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise TemperatureReadError(f"vcgencmd failed: {exc}") from exc
        match = _VCGENCMD_PATTERN.search(result.stdout or "")
        if result.returncode != 0 or not match:
            raise TemperatureReadError("vcgencmd returned no temperature")
        return float(match.group(1))


class TemperatureSource:
    """Detects a CPU sensor lazily, or uses an explicit sensor file."""

    def __init__(self, override: Optional[str] = None, thermal_root: Path = THERMAL_ROOT,
                 which: Callable = shutil.which, run: Callable = subprocess.run) -> None:
        self._override = override
        self._thermal_root = Path(thermal_root)
        self._which = which
        self._run = run
        self._sensor = None

    @property
    def description(self) -> str:
        if self._sensor is not None:
            return self._sensor.description
        return self._override or "auto detect"

    def _detect(self):
        if self._override:
            return SysfsSensor(Path(self._override).expanduser())
        primary = self._thermal_root / PRIMARY_ZONE / "temp"
        if primary.is_file():
            return SysfsSensor(primary)
        for zone in sorted(self._thermal_root.glob("thermal_zone*")):
            if "cpu" in _read_text(zone / "type").lower() and (zone / "temp").is_file():
                return SysfsSensor(zone / "temp")
        executable = self._which("vcgencmd")
        if executable:
            return VcgencmdSensor(executable, self._run)
        raise TemperatureReadError(
            "No CPU temperature source found (checked thermal_zone0 and vcgencmd)")

    def read_celsius(self) -> float:
        try:
            if self._sensor is None:
                self._sensor = self._detect()
            value = self._sensor.read_celsius()
        except TemperatureReadError:
            if not self._override:
                self._sensor = None
            raise
        if not is_plausible(value):
            raise TemperatureReadError(f"Implausible reading: {value}")
        return value
