"""The Arduino → Pi line protocol, built from ``serial.protocol`` in the config.

This is the single place the wire format is interpreted (CLAUDE.md §5). Nothing
downstream knows what the line looked like: a protocol turns one line of text
into ``{sensor_id: celsius}`` or into ``None`` for anything malformed.

Three shapes are supported, chosen by ``serial.protocol.format``:

``key_value``
    ``T1:23.44,T2:23.06,...`` — each reading carries its own id, so sensor order
    on the wire does not matter. The default.
``values``
    ``23.44,23.06,...`` — bare numbers, mapped positionally onto the configured
    ``sensors`` list. Shorter lines, but the order is a silent contract.
``regex``
    An escape hatch: a full-line regular expression whose *named groups* are
    sensor ids, for fixed-width, prefixed or otherwise unusual sketches.

Separators, whitespace tolerance, line prefixes/suffixes and the id/value
patterns are all configurable, so an Arduino sketch that emits ``T1: 23.4; T2:
23.1`` needs a config edit, not a code change.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Mapping, Sequence

from config.schema import ProtocolConfig, ProtocolFormat

_log = logging.getLogger(__name__)

__all__ = ["LineProtocol", "ProtocolError"]


class ProtocolError(ValueError):
    """Raised when the configured protocol cannot be turned into a parser."""


@dataclass(frozen=True, slots=True)
class LineProtocol:
    """A compiled parser for one configured wire format."""

    format: ProtocolFormat
    line_re: re.Pattern[str]
    field_re: re.Pattern[str] | None
    field_separator: str
    sensor_ids: tuple[str, ...]
    prefix: str
    suffix: str
    description: str

    # ------------------------------------------------------------------ building
    @classmethod
    def from_config(
        cls, config: ProtocolConfig, sensor_ids: Sequence[str]
    ) -> "LineProtocol":
        ids = tuple(sensor_ids)
        space = r"\s*" if config.allow_whitespace else ""
        separator = re.escape(config.field_separator)

        if config.format is ProtocolFormat.REGEX:
            if config.pattern is None:  # guarded by the config schema too
                raise ProtocolError("serial.protocol.pattern is required for format: regex")
            line_re = re.compile(config.pattern)
            field_re = None
            description = f"regex {config.pattern!r}"

        elif config.format is ProtocolFormat.KEY_VALUE:
            assign = re.escape(config.key_value_separator)
            field_re = re.compile(
                f"^{space}({config.id_pattern}){space}{assign}{space}"
                f"({config.value_pattern}){space}$"
            )
            one = f"{space}{config.id_pattern}{space}{assign}{space}{config.value_pattern}{space}"
            line_re = re.compile(f"^{one}(?:{separator}{one})*$")
            description = (
                f"key/value pairs separated by {config.field_separator!r}, "
                f"id{config.key_value_separator}value"
            )

        else:  # ProtocolFormat.VALUES
            field_re = re.compile(f"^{space}({config.value_pattern}){space}$")
            one = f"{space}{config.value_pattern}{space}"
            line_re = re.compile(f"^{one}(?:{separator}{one})*$")
            description = (
                f"{len(ids)} bare values separated by {config.field_separator!r}, "
                f"in the order of the sensors list"
            )

        return cls(
            format=config.format,
            line_re=line_re,
            field_re=field_re,
            field_separator=config.field_separator,
            sensor_ids=ids,
            prefix=config.line_prefix or "",
            suffix=config.line_suffix or "",
            description=description,
        )

    # ------------------------------------------------------------------ parsing
    def parse(self, raw: str) -> dict[str, float] | None:
        """Parse one line into ``{sensor_id: celsius}``.

        Returns ``None`` for anything malformed — truncated first reads after
        connecting, line noise, a half-written buffer. The caller logs and
        discards; a bad line must never raise or reach the GUI.
        """
        line = raw.strip()
        if self.prefix:
            if not line.startswith(self.prefix):
                return None
            line = line[len(self.prefix):]
        if self.suffix:
            if not line.endswith(self.suffix):
                return None
            line = line[: -len(self.suffix)]
        line = line.strip()
        if not line:
            return None

        if self.format is ProtocolFormat.REGEX:
            return self._parse_regex(line)
        if self.format is ProtocolFormat.KEY_VALUE:
            return self._parse_key_value(line)
        return self._parse_values(line)

    def _parse_key_value(self, line: str) -> dict[str, float] | None:
        # Cheap whole-line check first, then per-field, so a single bad field can
        # be pinpointed rather than the frame being discarded blindly.
        if not self.line_re.match(line):
            return None
        readings: dict[str, float] = {}
        for field in line.split(self.field_separator):
            match = self.field_re.match(field) if self.field_re else None
            if not match:
                return None
            readings[match.group(1)] = float(match.group(2))
        return readings

    def _parse_values(self, line: str) -> dict[str, float] | None:
        if not self.line_re.match(line):
            return None
        fields = line.split(self.field_separator)
        if len(fields) != len(self.sensor_ids):
            # Positional format: a different count means the sketch and the
            # sensors list disagree, and guessing an alignment would be wrong.
            return None
        return {
            sensor_id: float(field.strip())
            for sensor_id, field in zip(self.sensor_ids, fields)
        }

    def _parse_regex(self, line: str) -> dict[str, float] | None:
        match = self.line_re.match(line)
        if not match:
            return None
        try:
            return {name: float(value) for name, value in match.groupdict().items()}
        except (TypeError, ValueError):
            # A group matched something non-numeric (or did not participate).
            return None

    # ------------------------------------------------------------------ helpers
    def example(self, values: Mapping[str, float] | None = None) -> str:
        """A sample line in this format — used in logs and error messages."""
        sample = values or {
            sensor_id: 20.0 + index for index, sensor_id in enumerate(self.sensor_ids)
        }
        if self.format is ProtocolFormat.REGEX:
            return f"(matching {self.line_re.pattern})"
        if self.format is ProtocolFormat.KEY_VALUE:
            body = self.field_separator.join(
                f"{sensor_id}:{sample[sensor_id]:.2f}" for sensor_id in self.sensor_ids
            )
        else:
            body = self.field_separator.join(
                f"{sample[sensor_id]:.2f}" for sensor_id in self.sensor_ids
            )
        return f"{self.prefix}{body}{self.suffix}"

    def describe(self) -> str:
        return f"{self.format.value}: {self.description}"
