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
    "SerialConfig",
    "DemoConfig",
    "SensorConfig",
    "ModelExtent",
    "ModelConfig",
    "InterpolationConfig",
    "PlottingConfig",
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


class SerialConfig(_Section):
    port: str = Field(min_length=1)
    baud_rate: PositiveInt = 115200
    timeout_s: PositiveFloat = 1.0
    reconnect_interval_s: PositiveFloat = 3.0
    auto_detect: bool = True
    vid: UsbId | None = None
    pid: UsbId | None = None


class DemoConfig(_Section):
    csv_path: ProjectPath
    playback_rate_hz: PositiveFloat = 2.0
    loop: bool = True


class SensorConfig(_Section):
    id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    x: float
    y: float

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
        return self

    # ----------------------------------------------------------------- helpers
    @property
    def sensor_ids(self) -> tuple[str, ...]:
        return tuple(sensor.id for sensor in self.sensors)

    @property
    def sensor_ids_set(self) -> frozenset[str]:
        return frozenset(self.sensor_ids)

    def sensor(self, sensor_id: str) -> SensorConfig:
        for sensor in self.sensors:
            if sensor.id == sensor_id:
                return sensor
        raise KeyError(sensor_id)

    def positions(self) -> tuple[tuple[float, float], ...]:
        return tuple((sensor.x, sensor.y) for sensor in self.sensors)
