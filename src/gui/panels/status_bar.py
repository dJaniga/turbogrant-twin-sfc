"""Status bar: mode, connection state, sample rate, last error.

Errors must be *visible*, not just logged (CLAUDE.md §6), so every source error
lands here in addition to the log file.
"""

from __future__ import annotations

import time
from collections import deque

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel, QStatusBar, QWidget

from config.schema import Mode
from datasource.base import ConnectionState

__all__ = ["StatusBar"]

_STATE_COLORS = {
    ConnectionState.CONNECTED: "#59a14f",
    ConnectionState.CONNECTING: "#edc948",
    ConnectionState.PAUSED: "#edc948",
    ConnectionState.DISCONNECTED: "#999999",
    ConnectionState.ERROR: "#e15759",
}


class StatusBar(QStatusBar):
    """Permanent readouts on the right, transient messages on the left."""

    #: Samples used for the rolling rate estimate.
    _RATE_WINDOW = 20

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._frame_times: deque[float] = deque(maxlen=self._RATE_WINDOW)

        self._mode = self._add_label("Mode: —")
        self._source = self._add_label("Source: —")
        self._state = self._add_label("Disconnected")
        self._rate = self._add_label("0.0 Hz")

    def _add_label(self, text: str) -> QLabel:
        separator = QFrame(self)
        separator.setFrameShape(QFrame.Shape.VLine)
        separator.setFrameShadow(QFrame.Shadow.Sunken)
        self.addPermanentWidget(separator)
        label = QLabel(text, self)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.addPermanentWidget(label)
        return label

    # ------------------------------------------------------------------ updates
    def set_mode(self, mode: Mode) -> None:
        self._mode.setText(f"Mode: {mode.label}")

    def set_source(self, description: str) -> None:
        self._source.setText(f"Source: {description}")
        self._source.setToolTip(description)

    def set_state(self, state: ConnectionState) -> None:
        self._state.setText(state.value)
        self._state.setStyleSheet(f"color: {_STATE_COLORS.get(state, '#999999')};")
        if not state.is_active:
            self._frame_times.clear()
            self._rate.setText("0.0 Hz")

    def note_frame(self, timestamp: float | None = None) -> None:
        """Feed the rolling sample-rate estimate."""
        self._frame_times.append(timestamp if timestamp is not None else time.time())
        if len(self._frame_times) >= 2:
            span = self._frame_times[-1] - self._frame_times[0]
            rate = (len(self._frame_times) - 1) / span if span > 0 else 0.0
            self._rate.setText(f"{rate:.1f} Hz")

    def show_error(self, message: str, timeout_ms: int = 15_000) -> None:
        self.showMessage(f"⚠ {message}", timeout_ms)

    def show_info(self, message: str, timeout_ms: int = 5_000) -> None:
        self.showMessage(message, timeout_ms)
