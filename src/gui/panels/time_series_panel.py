"""Per-sensor rolling time-series plot (pyqtgraph).

Reads from a :class:`~processing.buffer.RollingBuffer` on a redraw timer instead
of redrawing on every incoming frame — decoupling ingest rate from draw rate is
what keeps this cheap on a Pi 5 (CLAUDE.md §9.3).
"""

from __future__ import annotations

from typing import Sequence

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QHBoxLayout, QSplitter, QWidget

from config.schema import AppTheme, PlottingConfig, SensorConfig
from datasource.models import SensorFrame
from processing.buffer import RollingBuffer
from processing.units import format_temperature, to_display
from ..colors import curve_colors
from ..theme import Theme, resolve as resolve_theme
from ..widgets.sensor_toggle_list import SensorToggleList

__all__ = ["TimeSeriesPanel"]


class TimeSeriesPanel(QWidget):
    """All sensors on one axis, one distinguishable curve each."""

    #: Smallest x-axis span while the buffer fills up, in seconds.
    _MIN_VISIBLE_SPAN_S = 30.0

    def __init__(
        self,
        sensors: Sequence[SensorConfig],
        plotting: PlottingConfig,
        theme: Theme | None = None,
        parent: QWidget | None = None,
        colors: Sequence[QColor] | None = None,
    ) -> None:
        """`colors`, one per sensor, overrides the colours derived from `plotting`
        — used to keep each sensor's colour stable when some are disabled."""
        super().__init__(parent)
        self._sensors = tuple(sensors)
        self._plotting = plotting
        self._theme = theme or resolve_theme(AppTheme.GRAY)
        self._colors = (
            list(colors)
            if colors is not None
            else curve_colors(len(self._sensors), plotting.curve_colors)
        )

        self._plot = pg.PlotWidget(axisItems={"bottom": pg.DateAxisItem(orientation="bottom")})
        self._plot.setBackground("default")
        # One level of minor ticks instead of two. The axis picture (grid
        # included) is regenerated on every frame because the range moves, and
        # the third tick level alone costs more than all ten curves together —
        # see the render notes in CLAUDE.md §9.3.
        for axis_name in ("bottom", "left"):
            self._plot.getAxis(axis_name).setStyle(maxTickLevel=1)
        self._plot.showGrid(x=True, y=True, alpha=self._theme.grid_alpha)
        self._plot.setLabel("left", "Temperature", units=plotting.temperature_units.suffix)
        self._plot.setLabel("bottom", "Time")
        self._legend = self._plot.addLegend(offset=(-10, 10), labelTextSize="8pt")
        self._plot.setClipToView(True)
        # The view is not user-manipulable: no pan, no zoom, no right-click menu
        # and no auto-range button. The x-axis always follows live data and the
        # y-axis auto-scales to what is on screen, so there is nothing to reset.
        self._plot.setMouseEnabled(x=False, y=False)
        self._plot.setMenuEnabled(False, False)
        self._plot.hideButtons()
        self._plot.scene().contextMenu = []  # drop pyqtgraph's "Export..." menu

        self._curves: dict[str, pg.PlotDataItem] = {}
        for sensor, color in zip(self._sensors, self._colors):
            # Pen width is a real performance setting, not just a look: a
            # 1-pixel cosmetic pen is drawn directly, anything wider has to be
            # stroked into polygons per segment — roughly twice the frame time
            # for ten curves.
            pen = pg.mkPen(color=color, width=plotting.curve_width)
            self._curves[sensor.id] = self._plot.plot(
                [],
                [],
                pen=pen,
                name=f"{sensor.id} — {sensor.label}",
                autoDownsample=True,
                skipFiniteCheck=True,  # frames are complete by construction
            )

        self._toggles = SensorToggleList(self._sensors, self._colors, self._theme, self)
        self._toggles.selection_changed.connect(self._on_toggle)

        # Sensor selection sits to the left of the plot; the plot takes all the
        # slack when the panel is resized.
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.addWidget(self._toggles)
        splitter.addWidget(self._plot)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([self._toggles.minimumWidth(), 900])

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)

        self.set_legend_visible(plotting.show_legend)

        self._drawn_revision = -1
        self._curves_dirty = True

    # ------------------------------------------------------------------- legend
    def set_legend_visible(self, visible: bool) -> None:
        """Show or hide the curve legend.

        The legend sits inside the plot and covers data, so it is optional: the
        sensor list on the right already names every curve and its colour.
        """
        if self._legend is not None:
            self._legend.setVisible(visible)

    def is_legend_visible(self) -> bool:
        return self._legend is not None and self._legend.isVisible()

    # ------------------------------------------------------------------ updates
    def update_from_buffer(self, buffer: RollingBuffer, now: float | None = None) -> None:
        """Redraw every visible curve from the rolling buffer.

        `now` is the wall-clock time the right edge should sit at. Passing it
        makes the axis slide continuously between samples, so the plot scrolls
        smoothly at the redraw rate instead of jumping once per sample. Pass
        ``None`` (paused/stopped) to freeze the window at the newest sample.
        """
        unit = self._plotting.temperature_units
        latest_time = buffer.latest_timestamp

        # Between samples the curves are unchanged and only the window moves, so
        # skip the data push entirely: pyqtgraph then reuses the paths it already
        # built. At 10 Hz redraw with 2 Hz data that is 8 frames out of 10.
        if buffer.revision != self._drawn_revision or self._curves_dirty:
            self._drawn_revision = buffer.revision
            self._curves_dirty = False
            for sensor in self._sensors:
                curve = self._curves[sensor.id]
                if not self._toggles.is_checked(sensor.id):
                    curve.setData([], [])
                    continue
                times, values = buffer.series(sensor.id)
                if times.size:
                    curve.setData(times, to_display(values, unit))
                else:
                    curve.setData([], [])

        if now is not None and latest_time is not None:
            latest_time = max(latest_time, now)
        if latest_time is not None:
            # Grow the visible span with the data instead of showing five minutes
            # of emptiness in the first minutes of a session.
            first_time = buffer.first_timestamp or latest_time
            span = min(
                self._plotting.time_window_s,
                max(self._MIN_VISIBLE_SPAN_S, latest_time - first_time),
            )
            self._plot.setXRange(latest_time - span, latest_time, padding=0.02)

    def update_readouts(self, frame: SensorFrame) -> None:
        """Refresh the numeric value shown beside each sensor's checkbox."""
        unit = self._plotting.temperature_units
        for sensor in self._sensors:
            value = frame.values.get(sensor.id, np.nan)
            self._toggles.set_value_text(
                sensor.id,
                format_temperature(value, unit),
                warn=sensor.id in frame.out_of_range,
            )

    def clear(self) -> None:
        for curve in self._curves.values():
            curve.setData([], [])
        for sensor in self._sensors:
            self._toggles.set_value_text(sensor.id, "--")
        self._curves_dirty = True

    # ---------------------------------------------------------------- internals
    def _on_toggle(self, sensor_id: str, visible: bool) -> None:
        curve = self._curves.get(sensor_id)
        if curve is not None and not visible:
            curve.setData([], [])
        # A sensor was shown again: its curve has to be repopulated on the next
        # redraw even if no new sample has arrived since.
        self._curves_dirty = True
