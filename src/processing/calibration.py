"""Per-sensor correction of readings against a reference thermometer.

Ten PT100/MAX31865 channels never agree exactly: each RTD has its own tolerance
class, each breakout its own reference resistor, and lead resistance differs per
cable. This module compensates for that in software, from coefficients in
``config.yaml`` — no Qt, no GUI knowledge (CLAUDE.md §3.3), so the same code can
correct a live frame or a recorded file.

Four correction types, cheapest first:

``offset``      ``corrected = raw + offset``
    Single-point calibration: put every sensor in one bath next to the reference
    and record the difference. Corrects the constant part, which dominates.
``linear``      ``corrected = gain * raw + offset``
    Two-point calibration (e.g. ice bath and a warm bath). Also corrects span,
    which matters when readings are compared across a wide range.
``polynomial``  ``corrected = c0 + c1*raw + c2*raw² + ...``
    For a channel with genuine curvature. Rarely needed for PT100s.
``table``       piecewise-linear interpolation of measured (raw, reference) pairs
    Multi-point calibration with no assumed model. The *correction* is
    interpolated, not the value, so outside the calibrated range the nearest
    known correction continues to apply instead of the reading being clamped.

Coefficients are best produced by ``scripts/calibrate.py`` from a recorded
session rather than by hand.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Mapping, Sequence

import numpy as np

from config.schema import CalibrationConfig, CalibrationType, SensorCalibration

_log = logging.getLogger(__name__)

__all__ = ["SensorCorrection", "Calibration"]


@dataclass(frozen=True, slots=True)
class SensorCorrection:
    """The correction for one sensor. ``apply`` is called once per reading."""

    sensor_id: str
    kind: CalibrationType = CalibrationType.NONE
    offset: float = 0.0
    gain: float = 1.0
    coefficients: tuple[float, ...] = ()
    table_raw: tuple[float, ...] = ()
    table_correction: tuple[float, ...] = ()
    note: str | None = None

    @classmethod
    def from_config(cls, sensor_id: str, config: SensorCalibration) -> "SensorCorrection":
        table_raw: tuple[float, ...] = ()
        table_correction: tuple[float, ...] = ()
        if config.type is CalibrationType.TABLE:
            ordered = sorted(config.points, key=lambda point: point[0])
            table_raw = tuple(point[0] for point in ordered)
            # Store the correction, not the reference: interpolating the
            # correction degrades gracefully outside the calibrated range.
            table_correction = tuple(point[1] - point[0] for point in ordered)
        return cls(
            sensor_id=sensor_id,
            kind=config.type,
            offset=config.offset,
            gain=config.gain,
            coefficients=tuple(config.coefficients),
            table_raw=table_raw,
            table_correction=table_correction,
            note=config.note,
        )

    @property
    def is_identity(self) -> bool:
        return self.kind is CalibrationType.NONE

    def apply(self, value: float) -> float:
        if self.kind is CalibrationType.NONE:
            return value
        if self.kind is CalibrationType.OFFSET:
            return value + self.offset
        if self.kind is CalibrationType.LINEAR:
            return self.gain * value + self.offset
        if self.kind is CalibrationType.POLYNOMIAL:
            corrected = 0.0
            for power, coefficient in enumerate(self.coefficients):
                corrected += coefficient * value**power
            return corrected
        return value + float(
            np.interp(value, self.table_raw, self.table_correction)
        )

    def describe(self) -> str:
        if self.kind is CalibrationType.NONE:
            return "none"
        if self.kind is CalibrationType.OFFSET:
            return f"offset {self.offset:+.3f} °C"
        if self.kind is CalibrationType.LINEAR:
            return f"linear gain {self.gain:.5f}, offset {self.offset:+.3f} °C"
        if self.kind is CalibrationType.POLYNOMIAL:
            return "polynomial " + ", ".join(f"{c:+.5g}" for c in self.coefficients)
        return f"table, {len(self.table_raw)} points"


@dataclass(frozen=True, slots=True)
class Calibration:
    """The whole calibration set: one :class:`SensorCorrection` per sensor."""

    corrections: dict[str, SensorCorrection] = field(default_factory=dict)
    reference: str | None = None

    @classmethod
    def from_config(
        cls, config: CalibrationConfig | None, sensor_ids: Sequence[str]
    ) -> "Calibration":
        """Build from config. Sensors without an entry are left uncorrected."""
        if config is None or not config.enabled:
            return cls()
        corrections = {
            sensor_id: SensorCorrection.from_config(sensor_id, config.sensors[sensor_id])
            for sensor_id in sensor_ids
            if sensor_id in config.sensors
        }
        return cls(
            corrections={k: v for k, v in corrections.items() if not v.is_identity},
            reference=config.reference,
        )

    @property
    def active(self) -> bool:
        return bool(self.corrections)

    def apply(
        self, values: Mapping[str, float], skip: frozenset[str] = frozenset()
    ) -> dict[str, float]:
        """Correct a whole frame.

        `skip` names sensors to pass through untouched — used for readings the
        source already knows are faulty, so a fault sentinel such as ``-999.00``
        stays exactly that instead of becoming ``-999.35``.
        """
        if not self.corrections:
            return dict(values)
        corrected = {}
        for sensor_id, value in values.items():
            correction = self.corrections.get(sensor_id)
            if correction is None or sensor_id in skip or not np.isfinite(value):
                corrected[sensor_id] = value
            else:
                corrected[sensor_id] = correction.apply(value)
        return corrected

    def describe(self) -> str:
        if not self.corrections:
            return "no calibration"
        kinds = sorted({c.kind.value for c in self.corrections.values()})
        text = f"{len(self.corrections)} sensor(s) calibrated ({', '.join(kinds)})"
        return f"{text}, reference: {self.reference}" if self.reference else text

    def log_summary(self) -> None:
        if not self.corrections:
            _log.info("Calibration: none configured — readings are used as received")
            return
        _log.info("Calibration active — %s", self.describe())
        for sensor_id, correction in self.corrections.items():
            _log.debug("  %s: %s", sensor_id, correction.describe())
