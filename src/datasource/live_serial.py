"""Live acquisition from the Arduino over USB serial.

Everything protocol-specific lives in this module — the regexes, the parser and
the port discovery. If the Arduino sketch changes (checksums, timestamps, more
sensors), only :func:`parse_line` and the two regexes below change; nothing
downstream is aware of the wire format (CLAUDE.md §5.1).

Serial I/O runs on a :class:`QThread`; frames reach the GUI thread through Qt's
queued signal delivery. No widget is ever touched from the worker.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Sequence

import serial
from serial.tools import list_ports

from PySide6.QtCore import QObject, QThread, Signal, Slot

from config.schema import SensorConfig, SensorLimits, SerialConfig
from .base import ConnectionState, DataSource

_log = logging.getLogger(__name__)

__all__ = ["FIELD_RE", "LINE_RE", "parse_line", "resolve_port", "LiveSerialSource"]


# --------------------------------------------------------------------------- #
# Protocol: `T1:23.4,T2:23.1,...,T10:24.0\n`
# --------------------------------------------------------------------------- #

#: One `key:value` pair. `T` + 1–2 digits, then a signed int/float. NaN, inf and
#: scientific notation are rejected on purpose: `Serial.print(float)` never emits
#: them, so seeing one means the line is garbage.
FIELD_RE = re.compile(r"^T(\d{1,2}):(-?\d+(?:\.\d+)?)$")

#: The whole line. Cardinality is *not* encoded here — it is checked against
#: ``len(config.sensors)`` separately, so adding an 11th sensor is config-only.
LINE_RE = re.compile(r"^T\d{1,2}:-?\d+(?:\.\d+)?(?:,T\d{1,2}:-?\d+(?:\.\d+)?)*$")

#: USB vendor ids commonly seen on Arduino boards and clones, used by
#: :func:`resolve_port` when ``serial.vid``/``serial.pid`` are not configured.
_KNOWN_ARDUINO_VIDS = (0x2341, 0x2A03, 0x1A86, 0x0403, 0x10C4, 0x2E8A)
_ARDUINO_HINTS = ("arduino", "ch340", "ch910", "usb serial", "usb-serial", "wch")


def parse_line(raw: str) -> dict[str, float] | None:
    """Parse one serial line into ``{sensor_id: celsius}``.

    Returns ``None`` for anything malformed — truncated first reads after
    connecting, line noise, a half-written buffer. The caller logs and discards;
    a bad line must never raise or reach the GUI.
    """
    line = raw.strip()
    if not LINE_RE.match(line):
        return None

    readings: dict[str, float] = {}
    for field in line.split(","):
        match = FIELD_RE.match(field)
        if not match:
            return None
        sensor_id, value = match.group(1), match.group(2)
        readings[f"T{sensor_id}"] = float(value)
    return readings


def resolve_port(config: SerialConfig) -> str:
    """Pick the device path to open.

    On Linux ``/dev/ttyACM0`` is not stable across reboots, so with
    ``auto_detect`` we scan for the Arduino's VID/PID first and only fall back to
    the configured path (CLAUDE.md §9.1).
    """
    if not config.auto_detect:
        return config.port

    candidates = list(list_ports.comports())
    for port in candidates:
        if config.vid is not None and port.vid != config.vid:
            continue
        if config.pid is not None and port.pid != config.pid:
            continue
        if config.vid is not None or config.pid is not None:
            _log.info("Auto-detected Arduino on %s (VID/PID match)", port.device)
            return port.device

    for port in candidates:
        haystack = " ".join(filter(None, (port.description, port.manufacturer, port.hwid))).lower()
        if port.vid in _KNOWN_ARDUINO_VIDS or any(hint in haystack for hint in _ARDUINO_HINTS):
            _log.info("Auto-detected Arduino on %s (%s)", port.device, port.description)
            return port.device

    _log.info("Auto-detect found no Arduino-like port; falling back to %s", config.port)
    return config.port


# --------------------------------------------------------------------------- #
# Worker
# --------------------------------------------------------------------------- #


class _SerialWorker(QObject):
    """Blocking read loop. Lives on its own thread, owns the serial handle."""

    values_ready = Signal(object, float)  # dict[str, float], timestamp
    state_changed = Signal(object)  # ConnectionState
    error = Signal(str)
    finished = Signal()

    #: Malformed lines are common on connect; don't flood the status bar.
    _BAD_LINE_REPORT_INTERVAL_S = 5.0

    def __init__(self, config: SerialConfig) -> None:
        super().__init__()
        self._config = config
        self._running = False
        self._port: serial.Serial | None = None
        self._bad_lines = 0
        self._last_bad_report = 0.0

    @Slot()
    def run(self) -> None:
        self._running = True
        while self._running:
            if self._port is None and not self._open_port():
                self._sleep_interruptible(self._config.reconnect_interval_s)
                continue
            self._read_once()
        self._close_port()
        self.state_changed.emit(ConnectionState.DISCONNECTED)
        self.finished.emit()

    def request_stop(self) -> None:
        """Called from the GUI thread; the loop exits within one read timeout."""
        self._running = False

    # ------------------------------------------------------------------ internals
    def _open_port(self) -> bool:
        self.state_changed.emit(ConnectionState.CONNECTING)
        device = resolve_port(self._config)
        try:
            self._port = serial.Serial(
                port=device,
                baudrate=self._config.baud_rate,
                timeout=self._config.timeout_s,
            )
        except (serial.SerialException, OSError) as exc:
            self._port = None
            self.state_changed.emit(ConnectionState.ERROR)
            self.error.emit(f"Cannot open {device}: {exc}")
            return False

        # The first line after opening is usually truncated (and many boards
        # reset on DTR), so drop whatever is already buffered.
        try:
            self._port.reset_input_buffer()
            self._port.readline()
        except (serial.SerialException, OSError):
            pass

        _log.info("Serial port open: %s @ %d baud", device, self._config.baud_rate)
        self.state_changed.emit(ConnectionState.CONNECTED)
        return True

    def _read_once(self) -> None:
        assert self._port is not None
        try:
            raw = self._port.readline()
        except (serial.SerialException, OSError) as exc:
            self._close_port()
            self.state_changed.emit(ConnectionState.ERROR)
            self.error.emit(f"Serial read failed, reconnecting: {exc}")
            self._sleep_interruptible(self._config.reconnect_interval_s)
            return

        if not raw:
            return  # read timeout — normal when the board is idle

        line = raw.decode("ascii", errors="replace")
        values = parse_line(line)
        if values is None:
            self._note_bad_line(line)
            return
        self.values_ready.emit(values, time.time())

    def _note_bad_line(self, line: str) -> None:
        self._bad_lines += 1
        _log.debug("Discarded malformed serial line: %r", line.strip())
        now = time.monotonic()
        if now - self._last_bad_report >= self._BAD_LINE_REPORT_INTERVAL_S:
            self._last_bad_report = now
            self.error.emit(
                f"Discarded {self._bad_lines} malformed serial line(s); "
                f"last: {line.strip()[:60]!r}"
            )

    def _close_port(self) -> None:
        if self._port is not None:
            try:
                self._port.close()
            except (serial.SerialException, OSError):
                pass
            self._port = None

    def _sleep_interruptible(self, seconds: float) -> None:
        """Sleep in slices so a stop request is honoured promptly."""
        deadline = time.monotonic() + seconds
        while self._running and time.monotonic() < deadline:
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))


# --------------------------------------------------------------------------- #
# Source
# --------------------------------------------------------------------------- #


class LiveSerialSource(DataSource):
    """Reads frames from the Arduino. Reconnects by itself if the cable moves."""

    def __init__(
        self,
        serial_config: SerialConfig,
        sensors: Sequence[SensorConfig],
        limits: SensorLimits | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(sensors, limits, parent)
        self._config = serial_config
        self._thread: QThread | None = None
        self._worker: _SerialWorker | None = None

    def describe(self) -> str:
        target = "auto-detect" if self._config.auto_detect else self._config.port
        return f"Serial {target} @ {self._config.baud_rate} baud"

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = QThread()
        self._thread.setObjectName("serial-reader")
        self._worker = _SerialWorker(self._config)
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.values_ready.connect(self._on_values)  # queued: worker → GUI
        self._worker.state_changed.connect(self._set_state)
        self._worker.error.connect(self._report_error)
        self._worker.finished.connect(self._thread.quit)

        self._set_state(ConnectionState.CONNECTING)
        self._thread.start()

    def stop(self) -> None:
        if self._worker is not None:
            self._worker.request_stop()
        if self._thread is not None:
            self._thread.quit()
            # Long enough for a blocked readline() to return on its timeout.
            if not self._thread.wait(int((self._config.timeout_s + 2.0) * 1000)):
                _log.warning("Serial thread did not stop cleanly; terminating")
                self._thread.terminate()
                self._thread.wait()
        self._thread = None
        self._worker = None
        self._set_state(ConnectionState.DISCONNECTED)

    @Slot(object, float)
    def _on_values(self, values: dict[str, float], timestamp: float) -> None:
        self._emit_values(values, timestamp)
