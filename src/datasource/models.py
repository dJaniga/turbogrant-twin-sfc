"""Data objects that flow from a :class:`~datasource.base.DataSource` to the GUI.

These are the *only* things downstream code sees. Nothing here records whether a
frame came from a serial port or a CSV replay — that is the central rule of this
project (CLAUDE.md §3).

Temperatures are always Celsius; conversion happens at display time
(``processing.units``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Iterator, Mapping, Protocol, Sequence, runtime_checkable

__all__ = ["SensorReading", "SensorFrame", "SensorLike"]


@runtime_checkable
class SensorLike(Protocol):
    """What :meth:`SensorFrame.readings` needs — satisfied by ``config.schema.SensorConfig``."""

    id: str
    label: str
    x: float
    y: float


@dataclass(frozen=True, slots=True)
class SensorReading:
    """A single sensor's value at a point in time, joined with its position.

    Built on demand from a :class:`SensorFrame` plus the configured sensor list;
    frames themselves stay position-free so the static geometry lives in exactly
    one place (the config).
    """

    sensor_id: str
    label: str
    value: float
    x: float
    y: float
    in_range: bool = True


@dataclass(frozen=True, slots=True)
class SensorFrame:
    """One complete sample: every configured sensor, one timestamp.

    Partial frames are never emitted — a source that cannot fill every sensor
    drops the sample and logs it instead (CLAUDE.md §5.1).
    """

    timestamp: float
    """Wall-clock epoch seconds (``time.time()``) of when the sample was taken."""

    values: Mapping[str, float]
    """``sensor_id -> temperature in Celsius``."""

    out_of_range: frozenset[str] = field(default_factory=frozenset)
    """Sensor ids whose value fell outside ``sensor_limits.value_range``."""

    def __post_init__(self) -> None:
        # Frames are handed to several panels; make the mapping unmodifiable so
        # one consumer cannot corrupt another's view of it.
        if not isinstance(self.values, MappingProxyType):
            object.__setattr__(self, "values", MappingProxyType(dict(self.values)))

    def __iter__(self) -> Iterator[tuple[str, float]]:
        return iter(self.values.items())

    def __len__(self) -> int:
        return len(self.values)

    @property
    def datetime(self) -> datetime:
        return datetime.fromtimestamp(self.timestamp, tz=timezone.utc).astimezone()

    def value_list(self, sensor_ids: Sequence[str]) -> list[float]:
        """Values in the given order — the order the config defines."""
        return [self.values[sensor_id] for sensor_id in sensor_ids]

    def readings(self, sensors: Sequence[SensorLike]) -> list[SensorReading]:
        """Join this frame with configured sensor metadata (id, label, x, y)."""
        return [
            SensorReading(
                sensor_id=sensor.id,
                label=sensor.label,
                value=self.values[sensor.id],
                x=sensor.x,
                y=sensor.y,
                in_range=sensor.id not in self.out_of_range,
            )
            for sensor in sensors
            if sensor.id in self.values
        ]
