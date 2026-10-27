"""Typed, validated representation of ``config/config.yaml`` (pydantic v2).

Nothing here imports Qt or any GUI code: the config layer must stay usable from
headless tooling (data generation, a future web dashboard, ...).

Every section is a frozen model with ``extra="forbid"``, so a mistyped key is an
error rather than a silently ignored setting. Validation is deliberately strict —
a typo in a sensor position should stop startup, not quietly become a default
(CLAUDE.md §4). :class:`~config.loader.load_config` turns pydantic's
``ValidationError`` into a :class:`ConfigError` carrying one line per problem.

Relative paths are resolved against the project root, which is supplied as
validation context: ``Config.model_validate(data, context={"root": ...})``.

Note that the acquisition dataclasses in ``datasource.models`` deliberately stay
plain dataclasses: those are built per sample from data the serial parser has
already validated, and frozen slotted dataclasses are cheaper per frame.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PositiveFloat,
    PositiveInt,
    ValidationInfo,
    field_validator,
    model_validator,
)

__all__ = [
    "ConfigError",
    "Mode",
    "TemperatureUnit",
    "InterpolationMethod",
    "AppTheme",
    "AppConfig",
    "ProtocolFormat",
    "ProtocolConfig",
    "SerialConfig",
    "DemoConfig",
    "SensorConfig",
    "ModelExtent",
    "ModelConfig",
    "InterpolationConfig",
    "PlottingConfig",
    "CalibrationType",
    "SensorCalibration",
    "CalibrationConfig",
    "ValueRange",
    "SensorLimits",
    "LoggingConfig",
    "LogoConfig",
    "SponsorAlignment",
    "SponsorPosition",
    "SponsorConfig",
    "Config",
]


class ConfigError(Exception):
    """Raised for any malformed, missing or inconsistent configuration value."""


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #


class Mode(str, Enum):
    """Data acquisition mode. Downstream code never branches on this."""

    LIVE = "live"
    DEMO = "demo"

    @property
    def label(self) -> str:
        return "Live" if self is Mode.LIVE else "Demo"


class TemperatureUnit(str, Enum):
    CELSIUS = "C"
    FAHRENHEIT = "F"

    @property
    def suffix(self) -> str:
        return "°C" if self is TemperatureUnit.CELSIUS else "°F"


class InterpolationMethod(str, Enum):
    RBF = "rbf"
    LINEAR = "linear"
    CUBIC = "cubic"
    NEAREST = "nearest"


class AppTheme(str, Enum):
    """Window and plot colour scheme. See ``gui.theme`` for the actual palettes."""

    GRAY = "gray"
    WHITE = "white"


# --------------------------------------------------------------------------- #
# Reusable annotated types
# --------------------------------------------------------------------------- #


def _resolve_against_root(value: Any, info: ValidationInfo) -> Any:
    """Make a configured path absolute, relative to the project root."""
    if not isinstance(value, (str, Path)):
        return value
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    root = (info.context or {}).get("root")
    return (Path(root) / path).resolve() if root is not None else path


def _lowercase(value: Any) -> Any:
    return value.lower() if isinstance(value, str) else value


def _uppercase(value: Any) -> Any:
    return value.upper() if isinstance(value, str) else value


def _usb_id(value: Any) -> Any:
    """USB VID/PID, accepted as int (``0x2341``) or hex string (``"2341"``)."""
    if isinstance(value, str):
        try:
            return int(value, 16)
        except ValueError as exc:
            raise ValueError(f"expected a hex USB id, got {value!r}") from exc
    return value


#: A path from the config file, resolved against the project root.
ProjectPath = Annotated[Path, BeforeValidator(_resolve_against_root)]

def _theme_name(value: Any) -> Any:
    """Accept either spelling of grey."""
    if isinstance(value, str):
        return "gray" if value.strip().lower() in ("grey", "gray") else value.lower()
    return value


CaseInsensitiveMode = Annotated[Mode, BeforeValidator(_lowercase)]
CaseInsensitiveTheme = Annotated[AppTheme, BeforeValidator(_theme_name)]
CaseInsensitiveMethod = Annotated[InterpolationMethod, BeforeValidator(_lowercase)]
CaseInsensitiveUnit = Annotated[TemperatureUnit, BeforeValidator(_uppercase)]
UsbId = Annotated[int, BeforeValidator(_usb_id)]


class _Section(BaseModel):
    """Base for every config section: immutable, strict about unknown keys."""

    model_config = ConfigDict(frozen=True, extra="forbid", protected_namespaces=())


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #


class AppConfig(_Section):
    title: str = "Sensor Monitor"
    default_mode: CaseInsensitiveMode = Mode.DEMO
    refresh_rate_hz: PositiveFloat = 10.0
    fullscreen: bool = False
    window_size: tuple[PositiveInt, PositiveInt] = (1400, 900)
    theme: CaseInsensitiveTheme = AppTheme.GRAY
    icon_path: ProjectPath | None = None
    """Window/taskbar icon. Any format Qt reads (.png, .ico, .svg); null = Qt default."""


class ProtocolFormat(str, Enum):
    """Shape of one line coming from the Arduino."""

    KEY_VALUE = "key_value"
    """``T1:23.44,T2:23.06,...`` — every reading carries its sensor id."""
    VALUES = "values"
    """``23.44,23.06,...`` — bare numbers, positional, in `sensors` order."""
    REGEX = "regex"
    """A full-line regular expression whose named groups are sensor ids."""


class ProtocolConfig(_Section):
    """How to read one line of serial data. See ``datasource.protocol``."""

    format: Annotated[ProtocolFormat, BeforeValidator(_lowercase)] = ProtocolFormat.KEY_VALUE
    field_separator: str = Field(default=",", min_length=1, max_length=4)
    key_value_separator: str = Field(default=":", min_length=1, max_length=4)
    allow_whitespace: bool = True
    """Tolerate spaces around separators, so ``T1:23.4, T2:23.1`` parses too."""
    id_pattern: str = r"T\d{1,2}"
    """Regex fragment for a sensor id (``key_value`` only)."""
    value_pattern: str = r"-?\d+(?:\.\d+)?"
    """Regex fragment for a reading. The default takes signed ints and decimals
    but rejects ``NaN``, ``inf`` and scientific notation on purpose: Arduino's
    ``Serial.print(float)`` never emits those, so seeing one means line noise."""
    line_prefix: str | None = None
    """Framing stripped before parsing, e.g. ``"DATA:"``."""
    line_suffix: str | None = None
    pattern: str | None = None
    """Full-line regex; required by (and only valid for) ``format: regex``."""

    @field_validator("id_pattern", "value_pattern", "pattern")
    @classmethod
    def _must_compile(cls, value: str | None) -> str | None:
        if value is None:
            return value
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"is not a valid regular expression: {exc}") from exc
        return value

    @model_validator(mode="after")
    def _check_combination(self) -> "ProtocolConfig":
        if self.field_separator == self.key_value_separator:
            raise ValueError("field_separator and key_value_separator must differ")
        if self.format is ProtocolFormat.REGEX:
            if not self.pattern:
                raise ValueError("format 'regex' requires `pattern`")
            if not re.compile(self.pattern).groupindex:
                raise ValueError(
                    "`pattern` needs one named group per sensor, e.g. (?P<T1>-?\\d+\\.\\d+)"
                )
        elif self.pattern is not None:
            raise ValueError(f"`pattern` only applies to format 'regex', not {self.format.value!r}")
        return self


class SerialConfig(_Section):
    port: str = Field(min_length=1)
    baud_rate: PositiveInt = 115200
    timeout_s: PositiveFloat = 1.0
    reconnect_interval_s: PositiveFloat = 3.0
    auto_detect: bool = True
    vid: UsbId | None = None
    pid: UsbId | None = None
    protocol: ProtocolConfig = ProtocolConfig()


class DemoConfig(_Section):
    csv_path: ProjectPath
    playback_rate_hz: PositiveFloat = 2.0
    loop: bool = True
    apply_calibration: bool = False
    """Recorded sessions already hold corrected values, so replaying one applies
    no calibration. Set true only when the CSV holds deliberately *raw* readings."""


class SensorConfig(_Section):
    id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    x: float
    y: float
    enabled: bool = True
    """Whether readings from this sensor are used at all. A disabled sensor stays in
    the list because the wire protocol still describes it (positional ``values``
    order, ``regex`` groups), but its readings are dropped on arrival: not
    calibrated, plotted, interpolated or recorded. Read once at startup."""

    @model_validator(mode="before")
    @classmethod
    def _default_label_to_id(cls, data: Any) -> Any:
        if isinstance(data, dict) and not data.get("label"):
            data = {**data, "label": data.get("id")}
        return data


@dataclass(frozen=True, slots=True)
class ModelExtent:
    """Plain geometry passed to ``processing.interpolation`` — deliberately not a
    pydantic model: it is derived from :class:`ModelConfig`, never parsed."""

    x_min: float
    x_max: float
    y_min: float
    y_max: float

    @property
    def width(self) -> float:
        return self.x_max - self.x_min

    @property
    def height(self) -> float:
        return self.y_max - self.y_min

    def contains(self, x: float, y: float) -> bool:
        return self.x_min <= x <= self.x_max and self.y_min <= y <= self.y_max


class ModelConfig(_Section):
    """Physical size of the model the sensors are mounted on.

    ``length`` runs along X, ``height`` along Y, both in the same units as the
    sensor coordinates. The model's origin is (0, 0), so the plotted area is
    exactly ``0..length`` by ``0..height`` — nothing outside it is ever drawn.
    """

    length: PositiveFloat
    height: PositiveFloat
    units: str = "cm"
    keep_aspect: bool = False
    background_image: ProjectPath | None = None

    @property
    def extent(self) -> ModelExtent:
        return ModelExtent(x_min=0.0, x_max=self.length, y_min=0.0, y_max=self.height)


class InterpolationConfig(_Section):
    method: CaseInsensitiveMethod = InterpolationMethod.RBF
    grid_resolution: int = Field(default=100, ge=8, le=1000)
    redraw_rate_hz: PositiveFloat = 2.0


class PlottingConfig(_Section):
    time_window_s: PositiveFloat = 300.0
    temperature_units: CaseInsensitiveUnit = TemperatureUnit.CELSIUS
    color_map: str = "turbo"
    curve_colors: tuple[str, ...] | None = None
    show_legend: bool = True
    """Startup state of the time-series legend; toggled at runtime from the toolbar."""
    curve_width: int = Field(default=1, ge=1, le=5)
    """Line width in px. 1 is a cosmetic pen and roughly twice as fast to draw as
    anything wider, which matters on the Pi — raise it only if the plot is being
    read from a distance."""


class ValueRange(_Section):
    min: float
    max: float

    @model_validator(mode="after")
    def _check_order(self) -> "ValueRange":
        if self.min >= self.max:
            raise ValueError("min must be smaller than max")
        return self


class CalibrationType(str, Enum):
    """How one sensor's readings are corrected. See ``processing.calibration``."""

    NONE = "none"
    OFFSET = "offset"
    """``raw + offset`` — single-point calibration."""
    LINEAR = "linear"
    """``gain * raw + offset`` — two-point calibration."""
    POLYNOMIAL = "polynomial"
    """``c0 + c1*raw + c2*raw² + …``"""
    TABLE = "table"
    """Piecewise-linear from measured ``[raw, reference]`` pairs."""


class SensorCalibration(_Section):
    type: Annotated[CalibrationType, BeforeValidator(_lowercase)] = CalibrationType.NONE
    offset: float = 0.0
    gain: float = 1.0
    coefficients: tuple[float, ...] = ()
    points: tuple[tuple[float, float], ...] = ()
    """``[[raw, reference], …]`` measured during a calibration run."""
    note: str | None = None
    """Free text — when and how this was measured. Worth filling in."""

    @model_validator(mode="after")
    def _check_for_type(self) -> "SensorCalibration":
        if self.type is CalibrationType.LINEAR and self.gain == 0:
            raise ValueError("linear calibration needs a non-zero gain")
        if self.type is CalibrationType.POLYNOMIAL and len(self.coefficients) < 2:
            raise ValueError("polynomial calibration needs at least two coefficients")
        if self.type is CalibrationType.TABLE:
            if len(self.points) < 2:
                raise ValueError("table calibration needs at least two [raw, reference] points")
            raws = [point[0] for point in self.points]
            if len(set(raws)) != len(raws):
                raise ValueError("table calibration has duplicate raw values")
        if self.type is not CalibrationType.TABLE and self.points:
            raise ValueError(f"`points` only applies to type 'table', not {self.type.value!r}")
        if self.type is not CalibrationType.POLYNOMIAL and self.coefficients:
            raise ValueError(
                f"`coefficients` only applies to type 'polynomial', not {self.type.value!r}"
            )
        return self


class CalibrationConfig(_Section):
    """Software compensation for per-sensor differences (CLAUDE.md §5.2)."""

    enabled: bool = True
    """Master switch: false uses every reading exactly as received."""
    reference: str | None = None
    """What the readings were calibrated against, e.g. 'Fluke 1524 / 5608 probe'."""
    sensors: dict[str, SensorCalibration] = {}
    """Sensor id → correction. Sensors without an entry are left uncorrected."""


class SensorLimits(_Section):
    """Plausibility band for readings; out-of-range values are flagged, not dropped."""

    value_range: ValueRange | None = None

    def is_in_range(self, value: float) -> bool:
        if self.value_range is None:
            return True
        return self.value_range.min <= value <= self.value_range.max


class LoggingConfig(_Section):
    enabled: bool = True
    output_dir: ProjectPath = Path("logs")
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_frames: bool = True
    include_raw: bool = True
    """Also write the uncorrected readings as ``<id>_raw`` columns when a
    calibration is active, so a session can be re-calibrated later. Demo replay
    ignores those columns."""

    _normalise_level = field_validator("level", mode="before")(_uppercase)


class SponsorPosition(str, Enum):
    """Which edge of the window the sponsor bar is docked to."""

    BOTTOM = "bottom"
    TOP = "top"
    LEFT = "left"
    RIGHT = "right"

    @property
    def is_vertical(self) -> bool:
        return self in (SponsorPosition.LEFT, SponsorPosition.RIGHT)


class SponsorAlignment(str, Enum):
    """Placement along the bar. On a vertical bar, left means top and right bottom."""

    LEFT = "left"
    CENTER = "center"
    RIGHT = "right"


class LogoConfig(_Section):
    path: ProjectPath
    link_url: str | None = None
    max_height_px: PositiveInt | None = None
    """Overrides ``sponsor.max_height_px`` for this logo (some marks need more room)."""


class SponsorConfig(_Section):
    logos: tuple[LogoConfig, ...] = ()
    position: Annotated[SponsorPosition, BeforeValidator(_lowercase)] = SponsorPosition.BOTTOM
    alignment: Annotated[SponsorAlignment, BeforeValidator(_lowercase)] = SponsorAlignment.CENTER
    max_height_px: PositiveInt = 60
    spacing_px: int = Field(default=24, ge=0, le=200)


# --------------------------------------------------------------------------- #
# Root
# --------------------------------------------------------------------------- #


class Config(_Section):
    """The whole validated configuration tree."""

    app: AppConfig = AppConfig()
    serial: SerialConfig
    demo: DemoConfig
    model: ModelConfig
    sensors: tuple[SensorConfig, ...] = Field(min_length=1)
    calibration: CalibrationConfig = CalibrationConfig()
    interpolation: InterpolationConfig = InterpolationConfig()
    plotting: PlottingConfig = PlottingConfig()
    sensor_limits: SensorLimits = SensorLimits()
    logging: LoggingConfig = LoggingConfig()
    sponsor: SponsorConfig = SponsorConfig()

    #: Filled in by the loader, not by the YAML file.
    project_root: Path
    source_path: Path

    # ------------------------------------------------------------- consistency
    @model_validator(mode="after")
    def _check_sensors(self) -> "Config":
        """Cross-section rules that no single field can express."""
        seen: dict[str, int] = {}
        for index, sensor in enumerate(self.sensors):
            if sensor.id in seen:
                raise ValueError(
                    f"sensors[{index}].id: duplicate sensor id {sensor.id!r} "
                    f"(already used by sensors[{seen[sensor.id]}])"
                )
            seen[sensor.id] = index

        if not any(sensor.enabled for sensor in self.sensors):
            raise ValueError("sensors: every sensor is disabled — enable at least one")

        extent = self.model.extent
        outside = [s.id for s in self.sensors if not extent.contains(s.x, s.y)]
        if outside:
            # A sensor off the model cannot be interpolated and is almost always
            # a units mix-up (mm vs cm), so this is an error, not a warning.
            raise ValueError(
                f"sensors: {', '.join(outside)} lie outside the model "
                f"(0..{self.model.length:g} × 0..{self.model.height:g} {self.model.units}) "
                f"— check model.length/model.height and the coordinate units"
            )

        positions: dict[tuple[float, float], str] = {}
        for sensor in self.sensors:
            key = (sensor.x, sensor.y)
            if key in positions:
                raise ValueError(
                    f"sensors: {sensor.id} and {positions[key]} share position {key}; "
                    "interpolation needs distinct sensor positions"
                )
            positions[key] = sensor.id

        if self.plotting.curve_colors is not None and len(self.plotting.curve_colors) < len(
            self.sensors
        ):
            raise ValueError(
                "plotting.curve_colors: fewer colours "
                f"({len(self.plotting.curve_colors)}) than sensors ({len(self.sensors)})"
            )

        unknown = sorted(set(self.calibration.sensors) - set(seen))
        if unknown:
            raise ValueError(
                f"calibration.sensors: no such sensor(s): {', '.join(unknown)} "
                f"(known ids: {', '.join(sorted(seen))})"
            )

        self._check_protocol()
        return self

    def _check_protocol(self) -> None:
        """The wire format has to name the same sensors the rest of the config does."""
        protocol = self.serial.protocol
        ids = {sensor.id for sensor in self.sensors}

        if protocol.format is ProtocolFormat.REGEX and protocol.pattern:
            groups = set(re.compile(protocol.pattern).groupindex)
            if groups != ids:
                missing = sorted(ids - groups)
                unexpected = sorted(groups - ids)
                detail = []
                if missing:
                    detail.append(f"no group for {', '.join(missing)}")
                if unexpected:
                    detail.append(f"unknown group(s) {', '.join(unexpected)}")
                raise ValueError(
                    "serial.protocol.pattern: named groups must match the sensor ids "
                    f"exactly — {'; '.join(detail)}"
                )

        if protocol.format is ProtocolFormat.KEY_VALUE:
            # A sensor whose id the id_pattern cannot match would make every
            # frame incomplete, and the app would just log discarded lines.
            id_re = re.compile(f"^(?:{protocol.id_pattern})$")
            unmatched = sorted(i for i in ids if not id_re.match(i))
            if unmatched:
                raise ValueError(
                    f"serial.protocol.id_pattern {protocol.id_pattern!r} does not match "
                    f"sensor id(s): {', '.join(unmatched)}"
                )

    # ----------------------------------------------------------------- helpers
    @property
    def sensor_ids(self) -> tuple[str, ...]:
        return tuple(sensor.id for sensor in self.sensors)

    @property
    def sensor_ids_set(self) -> frozenset[str]:
        return frozenset(self.sensor_ids)

    @property
    def active_sensors(self) -> tuple[SensorConfig, ...]:
        """The enabled sensors, in config order — what everything downstream uses."""
        return tuple(sensor for sensor in self.sensors if sensor.enabled)

    @property
    def active_sensor_ids(self) -> tuple[str, ...]:
        return tuple(sensor.id for sensor in self.active_sensors)

    def sensor(self, sensor_id: str) -> SensorConfig:
        for sensor in self.sensors:
            if sensor.id == sensor_id:
                return sensor
        raise KeyError(sensor_id)

    def positions(self) -> tuple[tuple[float, float], ...]:
        return tuple((sensor.x, sensor.y) for sensor in self.sensors)
