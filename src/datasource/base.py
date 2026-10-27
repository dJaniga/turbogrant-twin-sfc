"""The abstract data source every acquisition backend implements.

**The rule of this project**: nothing downstream of a :class:`DataSource` may know
whether frames arrived from an Arduino or from a CSV replay (CLAUDE.md §3). Both
implementations emit identical :class:`~datasource.models.SensorFrame` objects
through identical signals, so panels, buffers and loggers stay backend-agnostic.

Adding a third source later (a network feed, a simulator) means subclassing this
and registering it in ``gui.mode_controller`` — no panel changes.
"""

from __future__ import annotations

import logging
import time
from abc import ABCMeta, abstractmethod
from enum import Enum
from typing import Mapping, Sequence

from PySide6.QtCore import QObject, Signal

from config.schema import SensorConfig, SensorLimits
from processing.calibration import Calibration
from .models import SensorFrame

_log = logging.getLogger(__name__)

__all__ = ["ConnectionState", "DataSource"]


class _QObjectABCMeta(type(QObject), ABCMeta):
    """Qt's metaclass and ABCMeta combined, so a QObject can declare abstractmethods."""


class ConnectionState(Enum):
    """Lifecycle of a source, reported to the status bar."""

    DISCONNECTED = "Disconnected"
    CONNECTING = "Connecting"
    CONNECTED = "Connected"
    PAUSED = "Paused"
    ERROR = "Error"

    @property
    def is_active(self) -> bool:
        return self in (ConnectionState.CONNECTED, ConnectionState.PAUSED)


class DataSource(QObject, metaclass=_QObjectABCMeta):
    """Emits :class:`SensorFrame` objects at a defined cadence.

    Push model, not polling: subclasses emit :attr:`new_frame` from the GUI
    thread (worker threads must hop back via a queued signal connection).
    """

    new_frame = Signal(object)  # SensorFrame
    state_changed = Signal(object)  # ConnectionState
    error = Signal(str)  # human-readable, shown in the status bar

    #: Whether pause/resume/reset are meaningful for this source (Demo: yes).
    supports_playback: bool = False

    def __init__(
        self,
        sensors: Sequence[SensorConfig],
        limits: SensorLimits | None = None,
        calibration: Calibration | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        # Every configured sensor: the wire protocol describes all of them.
        self._sensors = tuple(sensors)
        self._sensor_ids = frozenset(s.id for s in self._sensors)
        # The enabled subset: the only readings that are ever emitted.
        self._active_sensors = tuple(s for s in self._sensors if s.enabled)
        self._active_ids = frozenset(s.id for s in self._active_sensors)
        self._limits = limits or SensorLimits(value_range=None)
        self._calibration = calibration or Calibration()
        self._state = ConnectionState.DISCONNECTED
        self._frames_emitted = 0

    # ---------------------------------------------------------------- lifecycle
    @abstractmethod
    def start(self) -> None:
        """Begin acquiring. Must not block the GUI thread."""

    @abstractmethod
    def stop(self) -> None:
        """Stop acquiring and release all resources (port, timers, threads)."""

    def is_connected(self) -> bool:
        return self._state.is_active

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def frames_emitted(self) -> int:
        return self._frames_emitted

    def describe(self) -> str:
        """Short human-readable description of where data is coming from."""
        return type(self).__name__

    # ------------------------------------------------------- playback (opt-in)
    def pause(self) -> None:
        """No-op unless :attr:`supports_playback`."""

    def resume(self) -> None:
        """No-op unless :attr:`supports_playback`."""

    def reset(self) -> None:
        """No-op unless :attr:`supports_playback`."""

    # ------------------------------------------------------------- for subclasses
    def _set_state(self, state: ConnectionState) -> None:
        if state is not self._state:
            self._state = state
            _log.debug("%s state → %s", type(self).__name__, state.name)
            self.state_changed.emit(state)

    def _report_error(self, message: str) -> None:
        _log.error("%s: %s", type(self).__name__, message)
        self.error.emit(message)

    def _emit_values(
        self, values: Mapping[str, float], timestamp: float | None = None
    ) -> bool:
        """Validate a complete set of readings and emit it as a frame.

        Readings from disabled sensors are dropped here, before anything else
        sees them. Returns ``False`` (and emits nothing) when an enabled sensor is
        missing or an id is not configured at all — a partial frame is worse than
        no frame, because it silently biases the interpolated field.
        """
        found = frozenset(values)
        unexpected = sorted(found - self._sensor_ids)
        missing = sorted(self._active_ids - found)
        if missing or unexpected:
            details = []
            if missing:
                details.append(f"missing {', '.join(missing)}")
            if unexpected:
                details.append(f"unexpected {', '.join(unexpected)}")
            self._report_error(f"Sample discarded: {'; '.join(details)}")
            return False
        if found != self._active_ids:
            values = {k: v for k, v in values.items() if k in self._active_ids}

        # Range check first, on the *raw* reading: a fault sentinel such as
        # -999.00 must be recognised as a fault, and must not be shifted by a
        # calibration offset into something that merely looks odd.
        out_of_range = frozenset(
            sensor_id
            for sensor_id, value in values.items()
            if not self._limits.is_in_range(value)
        )
        corrected = self._calibration.apply(values, skip=out_of_range)

        frame = SensorFrame(
            timestamp=timestamp if timestamp is not None else time.time(),
            values=corrected,
            raw_values=dict(values) if self._calibration.active else None,
            out_of_range=out_of_range,
        )
        self._frames_emitted += 1
        self.new_frame.emit(frame)
        return True
