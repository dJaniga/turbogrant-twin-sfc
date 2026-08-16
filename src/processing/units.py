"""Temperature unit conversion.

Storage, buffering, logging and interpolation are *always* Celsius; these helpers
are applied at the last moment before something is drawn or written to a label
(CLAUDE.md §6).
"""

from __future__ import annotations

import numpy as np

from config.schema import TemperatureUnit

__all__ = ["to_display", "format_temperature"]


def to_display(celsius, unit: TemperatureUnit):
    """Convert a float or numpy array from Celsius to the display unit."""
    if unit is TemperatureUnit.FAHRENHEIT:
        return celsius * 9.0 / 5.0 + 32.0
    return celsius


def format_temperature(celsius: float, unit: TemperatureUnit, decimals: int = 1) -> str:
    """``23.4 °C`` / ``74.1 °F``, or ``--`` for a missing reading."""
    if celsius is None or not np.isfinite(celsius):
        return "--"
    return f"{to_display(float(celsius), unit):.{decimals}f} {unit.suffix}"
