"""Checkbox list for showing/hiding individual sensors.

Reusable on purpose: any future panel that plots a per-sensor selection can drop
this in rather than growing its own toggle UI (CLAUDE.md §3.3).
"""

from __future__ import annotations

from typing import Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from config.schema import AppTheme, SensorConfig
from ..theme import Theme, resolve as resolve_theme

__all__ = ["SensorToggleList"]


class SensorToggleList(QWidget):
    """A checkbox per sensor, colour-swatched to match its curve."""

    selection_changed = Signal(str, bool)  # sensor_id, visible

    def __init__(
        self,
        sensors: Sequence[SensorConfig],
        colors: Sequence[QColor],
        theme: Theme | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._theme = theme or resolve_theme(AppTheme.GRAY)
        self._checkboxes: dict[str, QCheckBox] = {}
        self._value_labels: dict[str, QLabel] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(4)

        buttons = QHBoxLayout()
        for text, checked in (("All", True), ("None", False)):
            button = QPushButton(text, self)
            button.setFlat(True)
            button.clicked.connect(lambda _=False, state=checked: self.set_all(state))
            buttons.addWidget(button)
        outer.addLayout(buttons)

        container = QWidget(self)
        rows = QVBoxLayout(container)
        rows.setContentsMargins(0, 0, 0, 0)
        rows.setSpacing(2)

        for sensor, color in zip(sensors, colors):
            row = QHBoxLayout()
            row.setSpacing(6)

            swatch = QLabel(container)
            swatch.setFixedSize(12, 12)
            swatch.setStyleSheet(
                f"background-color: {color.name()}; "
                f"border: 1px solid {self._theme.swatch_border};"
            )
            row.addWidget(swatch)

            checkbox = QCheckBox(sensor.label or sensor.id, container)
            checkbox.setChecked(True)
            checkbox.setToolTip(f"{sensor.id} @ ({sensor.x:g}, {sensor.y:g})")
            checkbox.toggled.connect(
                lambda visible, sensor_id=sensor.id: self.selection_changed.emit(
                    sensor_id, visible
                )
            )
            self._checkboxes[sensor.id] = checkbox
            row.addWidget(checkbox, 1)

            value = QLabel("--", container)
            value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            value.setMinimumWidth(64)
            self._value_labels[sensor.id] = value
            row.addWidget(value)

            rows.addLayout(row)

        rows.addStretch(1)

        scroll = QScrollArea(self)
        scroll.setWidget(container)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        outer.addWidget(scroll, 1)
        # Wide enough that the live value ("-40.0 °C") is never clipped.
        self.setMinimumWidth(container.sizeHint().width() + 24)

    def is_checked(self, sensor_id: str) -> bool:
        checkbox = self._checkboxes.get(sensor_id)
        return checkbox is not None and checkbox.isChecked()

    def set_all(self, checked: bool) -> None:
        for checkbox in self._checkboxes.values():
            checkbox.setChecked(checked)

    def set_value_text(self, sensor_id: str, text: str, *, warn: bool = False) -> None:
        """Show the latest reading next to a sensor; `warn` marks it out of range."""
        label = self._value_labels.get(sensor_id)
        if label is None:
            return
        label.setText(text)
        label.setStyleSheet(
            f"color: {self._theme.warn_color}; font-weight: bold;" if warn else ""
        )
