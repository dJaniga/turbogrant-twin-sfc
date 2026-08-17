"""Application logging plus optional CSV recording of live sessions.

Recorded sessions use exactly the CSV layout Demo mode expects (a ``timestamp``
column plus one column per sensor id), so any live run can be replayed later
without conversion (CLAUDE.md §5).
"""

from __future__ import annotations

import csv
import logging
import logging.handlers
from datetime import datetime
from pathlib import Path
from typing import Sequence, TextIO

from config.schema import LoggingConfig
from datasource.models import SensorFrame

_log = logging.getLogger(__name__)

__all__ = ["configure_logging", "CsvFrameLogger"]

_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def configure_logging(config: LoggingConfig) -> None:
    """Set up console + rotating file logging. Safe to call once at startup."""
    root = logging.getLogger()
    root.setLevel(config.level)
    formatter = logging.Formatter(_LOG_FORMAT)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    if not config.enabled:
        return

    try:
        config.output_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            config.output_dir / "app.log",
            maxBytes=2_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
        _log.debug("Logging to %s", config.output_dir / "app.log")
    except OSError as exc:
        # A read-only or missing log directory must not stop the app.
        _log.warning("File logging disabled (%s)", exc)


class CsvFrameLogger:
    """Appends frames to ``logs/session_<timestamp>.csv`` while recording."""

    def __init__(
        self, sensor_ids: Sequence[str], output_dir: Path, include_raw: bool = True
    ) -> None:
        self._sensor_ids = tuple(sensor_ids)
        self._output_dir = output_dir
        self._include_raw = include_raw
        self._raw_columns: tuple[str, ...] = ()
        self._handle: TextIO | None = None
        self._writer: csv.writer | None = None
        self._path: Path | None = None
        self._rows = 0

    @property
    def is_recording(self) -> bool:
        return self._handle is not None

    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def rows(self) -> int:
        return self._rows

    def start(self, prefix: str = "session", calibrated: bool = False) -> Path:
        """Open a new session file. Raises :class:`OSError` if it cannot be created.

        `calibrated` adds ``<id>_raw`` columns alongside the corrected ones, so the
        session can be re-calibrated later; Demo replay reads only the plain
        columns and ignores them.
        """
        if self.is_recording:
            return self._path  # type: ignore[return-value]
        self._output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._path = self._output_dir / f"{prefix}_{stamp}.csv"
        self._handle = self._path.open("w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._handle)
        self._raw_columns = (
            tuple(f"{sensor_id}_raw" for sensor_id in self._sensor_ids)
            if (calibrated and self._include_raw)
            else ()
        )
        self._writer.writerow(["timestamp", *self._sensor_ids, *self._raw_columns])
        self._rows = 0
        _log.info("Recording frames to %s", self._path)
        return self._path

    def log_frame(self, frame: SensorFrame) -> None:
        if self._writer is None or self._handle is None:
            return
        timestamp = datetime.fromtimestamp(frame.timestamp).isoformat(timespec="milliseconds")
        row = [timestamp, *(f"{frame.values.get(s, float('nan')):.3f}" for s in self._sensor_ids)]
        if self._raw_columns:
            row.extend(f"{frame.raw(s):.3f}" for s in self._sensor_ids)
        self._writer.writerow(row)
        self._rows += 1
        if self._rows % 50 == 0:  # cheap durability without fsync per row
            self._handle.flush()

    def stop(self) -> Path | None:
        path = self._path
        if self._handle is not None:
            self._handle.flush()
            self._handle.close()
            _log.info("Stopped recording after %d rows: %s", self._rows, path)
        self._handle = None
        self._writer = None
        self._path = None
        return path
