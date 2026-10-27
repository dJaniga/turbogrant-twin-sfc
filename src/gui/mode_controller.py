"""Owns exactly one active :class:`DataSource` and swaps it on a mode change.

Panels connect to *this* object's signals once, at construction, and are never
re-wired: the controller re-emits whatever the current source produces. That is
what keeps the "panels never see a concrete source" rule cheap to obey
(CLAUDE.md §3.1) — and adding a third mode later means one entry in
:data:`_SOURCE_FACTORIES`, nothing else.
"""

from __future__ import annotations

import logging
from typing import Callable

from PySide6.QtCore import QObject, Signal, Slot

from config.schema import Config, Mode
from datasource.base import ConnectionState, DataSource
from datasource.demo_csv import DemoCSVSource
from datasource.live_serial import LiveSerialSource
from processing.calibration import Calibration

_log = logging.getLogger(__name__)

__all__ = ["ModeController"]


def _build_live(config: Config, parent: QObject) -> DataSource:
    calibration = Calibration.from_config(config.calibration, config.active_sensor_ids)
    calibration.log_summary()
    return LiveSerialSource(
        config.serial, config.sensors, config.sensor_limits, calibration, parent
    )


def _build_demo(config: Config, parent: QObject) -> DataSource:
    # Recorded sessions already hold corrected values, so Demo mode does *not*
    # calibrate by default — doing so would apply the correction twice. Set
    # `demo.apply_calibration: true` when replaying a deliberately raw file.
    calibration = (
        Calibration.from_config(config.calibration, config.active_sensor_ids)
        if config.demo.apply_calibration
        else Calibration()
    )
    return DemoCSVSource(
        config.demo, config.sensors, config.sensor_limits, calibration, parent
    )


#: Mode → factory. Register new acquisition backends here.
_SOURCE_FACTORIES: dict[Mode, Callable[[Config, QObject], DataSource]] = {
    Mode.LIVE: _build_live,
    Mode.DEMO: _build_demo,
}


class ModeController(QObject):
    """Mode switching, start/stop and playback control for the active source."""

    new_frame = Signal(object)  # SensorFrame
    state_changed = Signal(object)  # ConnectionState
    mode_changed = Signal(object)  # Mode
    error = Signal(str)

    def __init__(self, config: Config, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._mode = config.app.default_mode
        self._source: DataSource | None = None
        self._running = False

    # -------------------------------------------------------------------- state
    @property
    def mode(self) -> Mode:
        return self._mode

    @property
    def state(self) -> ConnectionState:
        return self._source.state if self._source else ConnectionState.DISCONNECTED

    @property
    def supports_playback(self) -> bool:
        return bool(self._source and self._source.supports_playback)

    @property
    def is_running(self) -> bool:
        return self._running

    def describe_source(self) -> str:
        return self._source.describe() if self._source else "—"

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> None:
        """Create the source for the current mode (if needed) and start it."""
        if self._source is None:
            self._create_source()
        assert self._source is not None
        self._running = True
        _log.info("Starting %s mode: %s", self._mode.value, self._source.describe())
        self._source.start()

    def stop(self) -> None:
        if self._source is not None:
            self._source.stop()
        self._running = False

    def set_mode(self, mode: Mode) -> None:
        """Switch acquisition mode, restarting acquisition if it was running."""
        if mode is self._mode and self._source is not None:
            return
        was_running = self._running
        self._teardown_source()
        self._mode = mode
        self._create_source()
        _log.info("Mode changed to %s", mode.value)
        self.mode_changed.emit(mode)
        self.state_changed.emit(ConnectionState.DISCONNECTED)
        if was_running:
            self.start()

    # ----------------------------------------------------------------- playback
    def pause(self) -> None:
        if self._source is not None:
            self._source.pause()

    def resume(self) -> None:
        if self._source is not None:
            self._source.resume()

    def reset(self) -> None:
        if self._source is not None:
            self._source.reset()

    # ---------------------------------------------------------------- internals
    def _create_source(self) -> None:
        factory = _SOURCE_FACTORIES.get(self._mode)
        if factory is None:  # unreachable while Mode and the table agree
            raise ValueError(f"No data source registered for mode {self._mode!r}")
        source = factory(self._config, self)
        source.new_frame.connect(self._on_frame)
        source.state_changed.connect(self._on_state)
        source.error.connect(self.error)
        self._source = source

    def _teardown_source(self) -> None:
        if self._source is None:
            return
        self._source.stop()
        self._source.new_frame.disconnect(self._on_frame)
        self._source.state_changed.disconnect(self._on_state)
        self._source.error.disconnect(self.error)
        self._source.deleteLater()
        self._source = None

    @Slot(object)
    def _on_frame(self, frame) -> None:
        self.new_frame.emit(frame)

    @Slot(object)
    def _on_state(self, state) -> None:
        self.state_changed.emit(state)

    def shutdown(self) -> None:
        """Stop and release the source — call from ``MainWindow.closeEvent``."""
        self._teardown_source()
        self._running = False
