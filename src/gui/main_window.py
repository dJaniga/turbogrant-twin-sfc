"""Application shell: docks, toolbar, sponsor area, and the frame pipeline.

Wiring, in one place so the flow is easy to follow:

    ModeController.new_frame ──► MainWindow._on_frame
                                   ├─► RollingBuffer (history)
                                   ├─► CsvFrameLogger (optional recording)
                                   └─► status bar rate estimate

    QTimer(app.refresh_rate_hz)          ──► TimeSeriesPanel.update_from_buffer
    QTimer(interpolation.redraw_rate_hz) ──► HeatmapPanel.update_field

Redraws are timer-driven rather than frame-driven so a fast Arduino cannot pin
the Pi's CPU (CLAUDE.md §9.3). Panels are registered through
:meth:`MainWindow._register_panel`; adding one is a new file plus one call.
"""

from __future__ import annotations

import logging
import time

import numpy as np
from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtGui import QAction, QActionGroup, QCloseEvent, QIcon, QKeySequence
from PySide6.QtWidgets import QApplication, QDockWidget, QMainWindow, QToolBar, QWidget

from config.schema import Config, Mode, SponsorPosition
from datasource.base import ConnectionState
from datasource.models import SensorFrame
from logging_utils.logger import CsvFrameLogger
from processing.buffer import RollingBuffer
from processing.calibration import Calibration
from . import theme
from .mode_controller import ModeController
from .panels.heatmap_panel import HeatmapPanel
from .panels.sponsor_panel import SponsorPanel
from .panels.status_bar import StatusBar
from .panels.time_series_panel import TimeSeriesPanel

_log = logging.getLogger(__name__)

__all__ = ["MainWindow"]


class MainWindow(QMainWindow):
    """The one window. Owns the controller, the buffer and the panels."""

    def __init__(self, config: Config, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self._latest_frame: SensorFrame | None = None
        self._field_dirty = False
        self._reported_out_of_range: frozenset[str] = frozenset()

        # Before any panel exists: pyqtgraph reads its background/foreground
        # options when a plot widget is constructed.
        self._theme = theme.apply(theme.resolve(config.app.theme))

        self.setWindowTitle(config.app.title)
        self.setDockNestingEnabled(True)
        self._apply_icon()

        self._buffer = RollingBuffer(
            sensor_ids=config.sensor_ids,
            window_s=config.plotting.time_window_s,
            # Sized by how fast data *arrives*, not how often the GUI redraws.
            expected_rate_hz=max(config.demo.playback_rate_hz, 5.0),
        )
        self._recorder = CsvFrameLogger(
            config.sensor_ids, config.logging.output_dir, config.logging.include_raw
        )
        self._calibration_active = Calibration.from_config(
            config.calibration, config.sensor_ids
        ).active

        self._status = StatusBar(self)
        self.setStatusBar(self._status)

        self._build_panels()
        self._build_sponsor_bar()
        self._build_toolbar()

        self._controller = ModeController(config, self)
        self._controller.new_frame.connect(self._on_frame)
        self._controller.state_changed.connect(self._on_state_changed)
        self._controller.mode_changed.connect(self._on_mode_changed)
        self._controller.error.connect(self._status.show_error)

        self._plot_timer = self._make_timer(config.app.refresh_rate_hz, self._redraw_series)
        self._heatmap_timer = self._make_timer(
            config.interpolation.redraw_rate_hz, self._redraw_heatmap
        )

        self._apply_geometry()
        self._sync_mode_actions(config.app.default_mode)
        self._status.set_mode(config.app.default_mode)
        self._status.set_state(ConnectionState.DISCONNECTED)

    # ------------------------------------------------------------------ startup
    def start(self) -> None:
        """Begin acquiring in the configured default mode."""
        self._sync_recording_to_mode(self._controller.mode)
        self._controller.start()
        self._status.set_source(self._controller.describe_source())
        self._plot_timer.start()
        self._heatmap_timer.start()
        self._update_action_states()

    # -------------------------------------------------------------------- build
    def _apply_icon(self) -> None:
        """Window and taskbar icon, from `app.icon_path` (already checked to exist)."""
        icon_path = self._config.app.icon_path
        if icon_path is None:
            return
        icon = QIcon(str(icon_path))
        if icon.isNull():
            _log.warning("Application icon %s could not be loaded", icon_path)
            return
        self.setWindowIcon(icon)
        app = QApplication.instance()
        if app is not None:
            app.setWindowIcon(icon)  # taskbar / window manager

    def _build_panels(self) -> None:
        self._time_series = TimeSeriesPanel(
            self._config.sensors, self._config.plotting, self._theme, self
        )
        self._heatmap = HeatmapPanel(
            self._config.sensors,
            self._config.model,
            self._config.interpolation,
            self._config.plotting,
            self._theme,
            self,
        )
        # No central widget: the docks share the whole window, so panels added
        # later slot in without redesigning the shell.
        time_dock = self._register_panel(
            "Time series", self._time_series, Qt.DockWidgetArea.LeftDockWidgetArea
        )
        field_dock = self._register_panel(
            "Temperature field", self._heatmap, Qt.DockWidgetArea.RightDockWidgetArea
        )
        window_width = self._config.app.window_size[0]
        self.resizeDocks(
            [time_dock, field_dock],
            [int(window_width * 0.55), int(window_width * 0.45)],
            Qt.Orientation.Horizontal,
        )

    def _register_panel(
        self, title: str, widget: QWidget, area: Qt.DockWidgetArea
    ) -> QDockWidget:
        """Add a panel as a dock. The single place new panels are hooked up."""
        dock = QDockWidget(title, self)
        dock.setObjectName(f"dock_{title.lower().replace(' ', '_')}")
        dock.setWidget(widget)
        dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
        )
        self.addDockWidget(area, dock)
        return dock

    #: sponsor.position → the toolbar area the branding strip is docked to.
    _SPONSOR_AREAS = {
        SponsorPosition.BOTTOM: Qt.ToolBarArea.BottomToolBarArea,
        SponsorPosition.TOP: Qt.ToolBarArea.TopToolBarArea,
        SponsorPosition.LEFT: Qt.ToolBarArea.LeftToolBarArea,
        SponsorPosition.RIGHT: Qt.ToolBarArea.RightToolBarArea,
    }

    def _build_sponsor_bar(self) -> None:
        self._sponsor_bar = QToolBar("Sponsor", self)
        self._sponsor_bar.setObjectName("sponsor_bar")
        self._sponsor_bar.setMovable(False)
        self._sponsor_bar.setFloatable(False)
        self._sponsor_bar.addWidget(SponsorPanel(self._config.sponsor, self))
        self.addToolBar(
            self._SPONSOR_AREAS[self._config.sponsor.position], self._sponsor_bar
        )

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Controls", self)
        toolbar.setObjectName("controls")
        toolbar.setMovable(False)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, toolbar)

        self._mode_actions: dict[Mode, QAction] = {}
        group = QActionGroup(self)
        group.setExclusive(True)
        for mode in (Mode.LIVE, Mode.DEMO):
            action = QAction(f"{mode.label} mode", self)
            action.setCheckable(True)
            action.triggered.connect(lambda _checked=False, m=mode: self._controller.set_mode(m))
            group.addAction(action)
            toolbar.addAction(action)
            self._mode_actions[mode] = action
        toolbar.addSeparator()

        self._start_action = QAction("Start", self)
        self._start_action.triggered.connect(self._on_start_clicked)
        toolbar.addAction(self._start_action)

        self._stop_action = QAction("Stop", self)
        self._stop_action.triggered.connect(self._on_stop_clicked)
        toolbar.addAction(self._stop_action)

        self._pause_action = QAction("Pause", self)
        self._pause_action.setCheckable(True)
        self._pause_action.toggled.connect(self._on_pause_toggled)
        toolbar.addAction(self._pause_action)

        self._reset_action = QAction("Restart replay", self)
        self._reset_action.triggered.connect(self._on_reset_clicked)
        toolbar.addAction(self._reset_action)
        toolbar.addSeparator()

        self._record_action = QAction("Record CSV", self)
        self._record_action.setCheckable(True)
        self._record_action.setToolTip(
            "Append incoming frames to logs/session_*.csv — replayable in Demo mode"
        )
        self._record_action.toggled.connect(self._on_record_toggled)
        toolbar.addAction(self._record_action)

        self._legend_action = QAction("Legend", self)
        self._legend_action.setCheckable(True)
        self._legend_action.setChecked(self._config.plotting.show_legend)
        self._legend_action.setShortcut(QKeySequence("Ctrl+L"))
        self._legend_action.setToolTip(
            "Show the curve legend on the time-series plot (plotting.show_legend)"
        )
        self._legend_action.toggled.connect(self._time_series.set_legend_visible)
        toolbar.addAction(self._legend_action)

        quit_action = QAction("Quit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        self.addAction(quit_action)

    def _make_timer(self, rate_hz: float, slot) -> QTimer:
        timer = QTimer(self)
        timer.setInterval(max(16, int(round(1000.0 / rate_hz))))
        timer.timeout.connect(slot)
        return timer

    def _apply_geometry(self) -> None:
        width, height = self._config.app.window_size
        self.resize(width, height)
        if self._config.app.fullscreen:
            self.showFullScreen()

    def createPopupMenu(self):  # noqa: N802 - Qt naming
        """Drop the sponsor bar from the dock/toolbar context menu — it must stay put."""
        menu = super().createPopupMenu()
        if menu is not None:
            for action in list(menu.actions()):
                if action is self._sponsor_bar.toggleViewAction():
                    menu.removeAction(action)
        return menu

    # ----------------------------------------------------------------- data flow
    @Slot(object)
    def _on_frame(self, frame: SensorFrame) -> None:
        self._latest_frame = frame
        self._field_dirty = True
        self._buffer.append(frame.timestamp, frame.values)
        self._status.note_frame(frame.timestamp)
        self._time_series.update_readouts(frame)

        if self._recorder.is_recording:
            self._recorder.log_frame(frame)

        # Report faults when the affected set changes, not on every frame.
        if frame.out_of_range != self._reported_out_of_range:
            self._reported_out_of_range = frame.out_of_range
            if frame.out_of_range:
                self._status.show_error(
                    "Reading(s) outside the plausible range: "
                    + ", ".join(sorted(frame.out_of_range))
                )

    @Slot()
    def _redraw_series(self) -> None:
        # While data is flowing the window slides with wall-clock time, so the
        # axis scrolls smoothly between samples instead of stepping once per
        # frame; when paused or stopped it freezes at the newest sample.
        live = self._controller.state is ConnectionState.CONNECTED
        self._time_series.update_from_buffer(self._buffer, time.time() if live else None)

    @Slot()
    def _redraw_heatmap(self) -> None:
        # The field only changes when a sample arrives, so redraw on demand
        # rather than on every tick — at 1 Hz live data that is half the work.
        if self._latest_frame is None or not self._field_dirty:
            return
        self._field_dirty = False
        values = np.array(
            [self._latest_frame.values.get(sensor_id, np.nan) for sensor_id in self._config.sensor_ids],
            dtype=float,
        )
        self._heatmap.update_field(values, self._latest_frame.out_of_range)

    # ------------------------------------------------------------------ controls
    @Slot()
    def _on_start_clicked(self) -> None:
        self._controller.start()
        self._status.set_source(self._controller.describe_source())
        self._pause_action.setChecked(False)
        self._plot_timer.start()
        self._heatmap_timer.start()
        self._update_action_states()

    @Slot()
    def _on_stop_clicked(self) -> None:
        self._controller.stop()
        self._plot_timer.stop()
        self._heatmap_timer.stop()
        self._update_action_states()

    @Slot(bool)
    def _on_pause_toggled(self, paused: bool) -> None:
        if paused:
            self._controller.pause()
        else:
            self._controller.resume()
        self._update_action_states()

    @Slot()
    def _on_reset_clicked(self) -> None:
        self._controller.reset()
        self._buffer.clear()
        self._time_series.clear()
        self._heatmap.clear()
        self._latest_frame = None
        self._status.show_info("Replay restarted")

    @Slot(bool)
    def _on_record_toggled(self, recording: bool) -> None:
        if recording:
            try:
                path = self._recorder.start(calibrated=self._calibration_active)
            except OSError as exc:
                self._record_action.setChecked(False)
                self._status.show_error(f"Cannot start recording: {exc}")
                return
            self._status.show_info(f"Recording to {path.name}")
        else:
            path = self._recorder.stop()
            if path is not None:
                self._status.show_info(f"Recording saved: {path.name} ({self._recorder.rows} rows)")

    # ------------------------------------------------------------------- signals
    @Slot(object)
    def _on_state_changed(self, state: ConnectionState) -> None:
        self._status.set_state(state)
        self._status.set_source(self._controller.describe_source())
        self._update_action_states()

    @Slot(object)
    def _on_mode_changed(self, mode: Mode) -> None:
        self._sync_mode_actions(mode)
        self._status.set_mode(mode)
        self._buffer.clear()
        self._time_series.clear()
        self._heatmap.clear()
        self._latest_frame = None

        self._sync_recording_to_mode(mode)
        self._update_action_states()

    def _sync_recording_to_mode(self, mode: Mode) -> None:
        """Record live sessions (if configured); never re-record replayed demo data."""
        wanted = (
            mode is Mode.LIVE
            and self._config.logging.enabled
            and self._config.logging.log_frames
        )
        if self._record_action.isChecked() != wanted:
            self._record_action.setChecked(wanted)

    def _sync_mode_actions(self, mode: Mode) -> None:
        for candidate, action in self._mode_actions.items():
            action.setChecked(candidate is mode)

    def _update_action_states(self) -> None:
        running = self._controller.is_running
        playback = self._controller.supports_playback
        self._start_action.setEnabled(not running)
        self._stop_action.setEnabled(running)
        self._pause_action.setEnabled(running and playback)
        self._reset_action.setEnabled(playback)
        self._record_action.setEnabled(self._config.logging.enabled)

    # -------------------------------------------------------------------- close
    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt naming
        _log.info("Shutting down")
        self._plot_timer.stop()
        self._heatmap_timer.stop()
        self._recorder.stop()
        self._controller.shutdown()
        super().closeEvent(event)
