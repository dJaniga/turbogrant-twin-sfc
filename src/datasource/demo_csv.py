"""Demo mode: replay a recorded CSV through the exact same pipeline as Live mode.

This is what makes development possible on a PC with no Arduino attached
(CLAUDE.md §9.6). Files written by ``logging_utils.logger`` are directly
replayable here — same columns, same order.

Rows are emitted with *fresh* wall-clock timestamps rather than the ones in the
file, so the time-series axis stays continuous across loops and behaves exactly
like a live feed.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd

from PySide6.QtCore import Qt, QObject, QTimer, Slot

from config.schema import DemoConfig, SensorConfig, SensorLimits
from processing.calibration import Calibration
from .base import ConnectionState, DataSource

_log = logging.getLogger(__name__)

__all__ = ["DemoCSVSource", "load_demo_frames"]


def load_demo_frames(csv_path: Path, sensor_ids: Sequence[str]) -> np.ndarray:
    """Read `csv_path` into an ``(n_rows, n_sensors)`` float array, column order = `sensor_ids`.

    Raises :class:`ValueError` with an actionable message — the caller surfaces it
    in the status bar instead of crashing.
    """
    try:
        table = pd.read_csv(csv_path)
    except OSError as exc:
        raise ValueError(f"Cannot read demo CSV {csv_path}: {exc}") from exc
    except pd.errors.ParserError as exc:
        raise ValueError(f"Demo CSV {csv_path} is not valid CSV: {exc}") from exc

    missing = [sensor_id for sensor_id in sensor_ids if sensor_id not in table.columns]
    if missing:
        raise ValueError(
            f"Demo CSV {csv_path.name} is missing column(s) for: {', '.join(missing)}. "
            f"Expected a header with a timestamp column plus one column per sensor id."
        )

    values = table.loc[:, list(sensor_ids)].apply(pd.to_numeric, errors="coerce")
    complete = values.notna().all(axis=1)
    dropped = int((~complete).sum())
    if dropped:
        _log.warning("Demo CSV %s: skipped %d row(s) with missing/non-numeric values",
                     csv_path.name, dropped)
    values = values[complete]
    if values.empty:
        raise ValueError(f"Demo CSV {csv_path.name} contains no usable rows")
    return values.to_numpy(dtype=float)


class DemoCSVSource(DataSource):
    """Replays a CSV at a configured rate, looping at EOF."""

    supports_playback = True

    def __init__(
        self,
        demo_config: DemoConfig,
        sensors: Sequence[SensorConfig],
        limits: SensorLimits | None = None,
        calibration: Calibration | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(sensors, limits, calibration, parent)
        self._config = demo_config
        self._rows: np.ndarray | None = None
        self._index = 0
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(max(1, int(round(1000.0 / demo_config.playback_rate_hz))))
        self._timer.timeout.connect(self._tick)

    def describe(self) -> str:
        rows = "?" if self._rows is None else str(len(self._rows))
        return (
            f"Demo replay {self._config.csv_path.name} "
            f"({rows} rows @ {self._config.playback_rate_hz:g} Hz"
            f"{', looping' if self._config.loop else ''})"
        )

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._rows is None:
            self._set_state(ConnectionState.CONNECTING)
            try:
                self._rows = load_demo_frames(self._config.csv_path, self._sensor_ids_ordered)
            except ValueError as exc:
                self._rows = None
                self._set_state(ConnectionState.ERROR)
                self._report_error(str(exc))
                return
            _log.info("Loaded %d demo rows from %s", len(self._rows), self._config.csv_path)
        self._timer.start()
        self._set_state(ConnectionState.CONNECTED)

    def stop(self) -> None:
        self._timer.stop()
        self._set_state(ConnectionState.DISCONNECTED)

    # ----------------------------------------------------------------- playback
    def pause(self) -> None:
        if self._timer.isActive():
            self._timer.stop()
            self._set_state(ConnectionState.PAUSED)

    def resume(self) -> None:
        if self._rows is not None and not self._timer.isActive():
            self._timer.start()
            self._set_state(ConnectionState.CONNECTED)

    def reset(self) -> None:
        self._index = 0
        _log.debug("Demo playback reset to first row")

    @property
    def progress(self) -> tuple[int, int]:
        return self._index, 0 if self._rows is None else len(self._rows)

    # ---------------------------------------------------------------- internals
    @property
    def _sensor_ids_ordered(self) -> tuple[str, ...]:
        return tuple(sensor.id for sensor in self._sensors)

    @Slot()
    def _tick(self) -> None:
        if self._rows is None or len(self._rows) == 0:
            return
        if self._index >= len(self._rows):
            if not self._config.loop:
                self._timer.stop()
                self._set_state(ConnectionState.DISCONNECTED)
                _log.info("Demo playback finished (loop disabled)")
                return
            self._index = 0

        row = self._rows[self._index]
        self._index += 1
        values = dict(zip(self._sensor_ids_ordered, (float(v) for v in row)))
        self._emit_values(values, time.time())
