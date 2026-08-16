"""2-D interpolated temperature field over the physical model.

The panel owns a :class:`~processing.interpolation.FieldInterpolator` (which
knows nothing about Qt) and is redrawn from a timer in ``MainWindow``, not on
every frame — see CLAUDE.md §9.3.
"""

from __future__ import annotations

import logging
from typing import Sequence

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QRectF
from PySide6.QtGui import QPixmap, QTransform
from PySide6.QtWidgets import QGraphicsPixmapItem, QVBoxLayout, QWidget

from config.schema import (
    AppTheme,
    InterpolationConfig,
    ModelConfig,
    PlottingConfig,
    SensorConfig,
)
from processing.interpolation import FieldInterpolator
from processing.units import format_temperature, to_display
from ..colors import resolve_colormap
from ..theme import Theme, resolve as resolve_theme

_log = logging.getLogger(__name__)

__all__ = ["HeatmapPanel"]


class HeatmapPanel(QWidget):
    """Interpolated field + sensor markers, drawn in physical model coordinates."""

    def __init__(
        self,
        sensors: Sequence[SensorConfig],
        model: ModelConfig,
        interpolation: InterpolationConfig,
        plotting: PlottingConfig,
        theme: Theme | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._sensors = tuple(sensors)
        self._plotting = plotting
        self._theme = theme or resolve_theme(AppTheme.GRAY)
        self._model = model
        self._extent = model.extent
        self._interpolator = FieldInterpolator.from_config(
            np.array([(s.x, s.y) for s in self._sensors], dtype=float),
            self._extent,
            interpolation,
        )

        self._layout_widget = pg.GraphicsLayoutWidget()
        self._plot = self._layout_widget.addPlot(row=0, col=0)
        self._plot.setLabel("bottom", "X", units=model.units)
        self._plot.setLabel("left", "Y", units=model.units)
        self._configure_view()

        self._background_item = self._make_background(model.background_image)
        has_background = self._background_item is not None

        self._image_rect = QRectF(
            self._extent.x_min,
            self._extent.y_min,
            self._extent.width,
            self._extent.height,
        )
        self._image = pg.ImageItem()
        self._image.setOpacity(0.75 if has_background else 1.0)
        self._image.setZValue(0)
        self._plot.addItem(self._image)

        self._colorbar = pg.ColorBarItem(
            colorMap=resolve_colormap(plotting.color_map),
            interactive=False,
            label=f"Temperature ({plotting.temperature_units.suffix})",
        )
        self._colorbar.setImageItem(self._image, insert_in=self._plot)

        self._markers = pg.ScatterPlotItem(
            size=11,
            pen=pg.mkPen(self._theme.marker_pen, width=1),
            brush=pg.mkBrush(*self._theme.marker_brush),
            hoverable=True,
            tip=self._describe_sensor,
        )
        self._markers.setZValue(10)
        self._plot.addItem(self._markers)
        # Positions never change, so the marker data is set exactly once; the
        # tooltip is built on hover from the latest values instead of being
        # rewritten into every spot on every frame.
        self._markers.setData(
            pos=[(s.x, s.y) for s in self._sensors],
            data=list(range(len(self._sensors))),
        )
        self._latest_values: dict[str, float] = {}

        self._labels: dict[str, pg.TextItem] = {}
        self._label_texts: dict[str, str] = {}
        self._label_colors: dict[str, str] = {}
        for sensor in self._sensors:
            label = pg.TextItem(
                text=sensor.id,
                color=self._theme.label_color,
                anchor=(0.5, 1.3),
                fill=pg.mkBrush(*self._theme.label_fill),
            )
            label.setPos(sensor.x, sensor.y)
            label.setZValue(11)
            self._plot.addItem(label)
            self._labels[sensor.id] = label

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._layout_widget)

    # ------------------------------------------------------------------ updates
    def update_field(self, values_celsius: np.ndarray, out_of_range: frozenset[str]) -> None:
        """Redraw from one sample. `values_celsius` is in configured sensor order."""
        values = np.asarray(values_celsius, dtype=float)
        if not np.isfinite(values).any():
            return

        field = self._interpolator.interpolate(values)
        unit = self._plotting.temperature_units
        display_field = to_display(field, unit)

        # Scale the colour bar to the *measured* range, not the interpolated one:
        # RBF extrapolation outside the sensor hull overshoots badly, and letting
        # those corner values drive the levels flattens the whole field to one
        # colour. Values are clipped to the same range for the same reason.
        measured = to_display(values[np.isfinite(values)], unit)
        low, high = float(measured.min()), float(measured.max())
        if high - low < 0.5:  # avoid a jittery colour bar on a flat field
            mid = (high + low) / 2.0
            low, high = mid - 0.25, mid + 0.25
        self._colorbar.setLevels(low=low, high=high)

        # The rect is (re)applied with the data: ImageItem derives its transform
        # from the current image shape, so setting it on an empty item would
        # stretch a single pixel across the whole model.
        self._image.setImage(np.clip(display_field, low, high), autoLevels=False)
        self._image.setRect(self._image_rect)
        self._update_markers(values, out_of_range)

    def clear(self) -> None:
        self._image.clear()
        self._latest_values.clear()
        for sensor in self._sensors:
            self._labels[sensor.id].setText(sensor.id)
            self._label_texts[sensor.id] = sensor.id

    def _update_markers(self, values: np.ndarray, out_of_range: frozenset[str]) -> None:
        """Refresh the on-model labels, skipping any that would not change.

        Each ``setText`` re-lays out rich text in Qt, so repeating an identical
        string is pure cost — common when readings are stable to 0.1°.
        """
        unit = self._plotting.temperature_units
        for sensor, value in zip(self._sensors, values):
            self._latest_values[sensor.id] = float(value)
            label = self._labels[sensor.id]
            text = f"{sensor.id}  {format_temperature(value, unit)}"
            if text != self._label_texts.get(sensor.id):
                self._label_texts[sensor.id] = text
                label.setText(text)
            color = (
                self._theme.warn_color
                if sensor.id in out_of_range
                else self._theme.label_color
            )
            if color != self._label_colors.get(sensor.id):
                self._label_colors[sensor.id] = color
                label.setColor(color)

    def _describe_sensor(self, x: float, y: float, data: int) -> str:
        """Tooltip text, built only when a marker is actually hovered."""
        sensor = self._sensors[int(data)]
        value = self._latest_values.get(sensor.id, float("nan"))
        return (
            f"{sensor.id} — {sensor.label}\n"
            f"{format_temperature(value, self._plotting.temperature_units)}\n"
            f"({sensor.x:g}, {sensor.y:g}) {self._model.units}"
        )

    # ---------------------------------------------------------------- internals
    def _configure_view(self) -> None:
        """Show the model and nothing else — the plot area is the model area.

        The view is pinned to ``0..length × 0..height`` with zero padding, so no
        blank margin is ever drawn around the field. With ``model.keep_aspect``
        the true proportions are preserved instead, which reintroduces bars on
        one side whenever the panel's shape differs from the model's.
        """
        view = self._plot.getViewBox()
        view.setDefaultPadding(0.0)
        view.setAspectLocked(self._model.keep_aspect)
        view.disableAutoRange()
        # Fixed view: the panel always shows the whole model and nothing else, so
        # pan, zoom, the right-click menu and the auto-range button are all off.
        view.setMouseEnabled(x=False, y=False)
        self._plot.setMenuEnabled(False, False)
        self._plot.hideButtons()
        self._plot.scene().contextMenu = []  # drop pyqtgraph's "Export..." menu
        self._plot.setXRange(self._extent.x_min, self._extent.x_max, padding=0.0)
        self._plot.setYRange(self._extent.y_min, self._extent.y_max, padding=0.0)

    def _make_background(self, path) -> QGraphicsPixmapItem | None:
        """Place the optional model layout image behind the field, in model units."""
        if path is None:
            return None
        pixmap = QPixmap(str(path))
        if pixmap.isNull() or pixmap.width() == 0 or pixmap.height() == 0:
            _log.warning("Background image %s could not be loaded; ignoring it", path)
            return None

        item = QGraphicsPixmapItem(pixmap)
        # Map pixmap pixels onto the model extent, flipping y (image y grows
        # downwards, plot y grows upwards).
        transform = QTransform()
        transform.translate(self._extent.x_min, self._extent.y_max)
        transform.scale(
            self._extent.width / pixmap.width(), -self._extent.height / pixmap.height()
        )
        item.setTransform(transform)
        item.setZValue(-100)
        self._plot.addItem(item)
        return item
