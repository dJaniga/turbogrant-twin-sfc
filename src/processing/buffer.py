"""Rolling per-sensor time-series storage.

No Qt imports here (CLAUDE.md §3.3/§6): buffering must stay reusable by anything
that consumes frames — the GUI today, a headless recorder or web dashboard later.

Length is capped by the configured time window rather than growing unbounded,
which matters on the Pi's limited RAM (CLAUDE.md §9.3).
"""

from __future__ import annotations

from collections import deque
from typing import Iterable, Mapping, Sequence

import numpy as np

__all__ = ["RollingBuffer"]


class RollingBuffer:
    """Fixed-duration history of every sensor, plus the latest complete sample."""

    #: Hard cap per sensor, derived from the window and expected rate, so a
    #: misconfigured (very fast) source cannot exhaust memory.
    _SAFETY_FACTOR = 4

    def __init__(
        self,
        sensor_ids: Sequence[str],
        window_s: float,
        expected_rate_hz: float = 10.0,
        max_points: int | None = None,
    ) -> None:
        if window_s <= 0:
            raise ValueError("window_s must be greater than 0")
        self._sensor_ids = tuple(sensor_ids)
        self._window_s = float(window_s)
        cap = max_points or int(window_s * max(expected_rate_hz, 1.0) * self._SAFETY_FACTOR)
        self._max_points = max(cap, 16)
        self._times: deque[float] = deque(maxlen=self._max_points)
        self._series: dict[str, deque[float]] = {
            sensor_id: deque(maxlen=self._max_points) for sensor_id in self._sensor_ids
        }
        self._latest: dict[str, float] = {}
        self._latest_timestamp: float | None = None
        self._revision = 0

    # ------------------------------------------------------------------ writing
    def append(self, timestamp: float, values: Mapping[str, float]) -> None:
        """Add one complete sample. Unknown sensor ids are ignored."""
        self._times.append(float(timestamp))
        for sensor_id, series in self._series.items():
            series.append(float(values.get(sensor_id, np.nan)))
        self._latest = {k: float(v) for k, v in values.items()}
        self._latest_timestamp = float(timestamp)
        self._revision += 1
        self._trim(timestamp)

    def clear(self) -> None:
        self._times.clear()
        for series in self._series.values():
            series.clear()
        self._latest = {}
        self._latest_timestamp = None
        self._revision += 1

    @property
    def revision(self) -> int:
        """Bumped on every change. Lets consumers skip work when nothing is new."""
        return self._revision

    def _trim(self, now: float) -> None:
        cutoff = now - self._window_s
        while self._times and self._times[0] < cutoff:
            self._times.popleft()
            for series in self._series.values():
                if series:
                    series.popleft()

    # ------------------------------------------------------------------ reading
    @property
    def sensor_ids(self) -> tuple[str, ...]:
        return self._sensor_ids

    @property
    def window_s(self) -> float:
        return self._window_s

    @property
    def latest_timestamp(self) -> float | None:
        return self._latest_timestamp

    @property
    def first_timestamp(self) -> float | None:
        """Oldest timestamp still inside the window (used to size the x-axis)."""
        return self._times[0] if self._times else None

    def latest(self) -> dict[str, float]:
        """Most recent sample, or ``{}`` before the first frame arrives."""
        return dict(self._latest)

    def latest_values(self, sensor_ids: Iterable[str] | None = None) -> np.ndarray:
        """Latest values in config order, NaN where a sensor has not reported."""
        ids = tuple(sensor_ids) if sensor_ids is not None else self._sensor_ids
        return np.array([self._latest.get(sensor_id, np.nan) for sensor_id in ids], dtype=float)

    def series(self, sensor_id: str) -> tuple[np.ndarray, np.ndarray]:
        """``(timestamps, values)`` for one sensor, oldest first."""
        series = self._series.get(sensor_id)
        if not series:
            return np.empty(0, dtype=float), np.empty(0, dtype=float)
        # Timestamps may be one entry longer only mid-append; slice to be safe.
        count = min(len(self._times), len(series))
        times = np.fromiter(self._times, dtype=float, count=len(self._times))[-count:]
        values = np.fromiter(series, dtype=float, count=len(series))[-count:]
        return times, values

    def __len__(self) -> int:
        return len(self._times)
