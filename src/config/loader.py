"""Load, validate and cross-check ``config/config.yaml``.

Shape and value validation live in the pydantic models (``schema.py``); this
module owns everything around them: reading the file, resolving the project root,
turning ``ValidationError`` into a readable :class:`ConfigError`, and the checks
that touch the filesystem.

Fail fast and loudly: a bad sensor position or a missing demo CSV must stop
startup with a message naming the exact key, never fall back to a default
(CLAUDE.md §4).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Mapping

import yaml
from pydantic import ValidationError

from .schema import Config, ConfigError

_log = logging.getLogger(__name__)

#: Repository root — ``src/config/loader.py`` → ``src/config`` → ``src`` → root.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"


def load_config(path: str | Path | None = None) -> Config:
    """Read `path` (default ``config/config.yaml``) into a validated :class:`Config`.

    Raises :class:`ConfigError` for anything the app cannot sensibly run with.
    """
    config_path = Path(path).expanduser().resolve() if path else DEFAULT_CONFIG_PATH
    raw = _read_yaml(config_path)
    _reject_legacy_keys(raw)

    try:
        config = Config.model_validate(
            {**raw, "project_root": PROJECT_ROOT, "source_path": config_path},
            context={"root": PROJECT_ROOT},
        )
    except ValidationError as exc:
        raise ConfigError(f"{config_path}:\n  " + _describe(exc)) from exc

    _check_referenced_files(config)
    _warn_about_rates(config)
    _log.info(
        "Loaded configuration from %s (%d sensors, %d active, model %g × %g %s, "
        "default mode: %s)",
        config_path,
        len(config.sensors),
        len(config.active_sensors),
        config.model.length,
        config.model.height,
        config.model.units,
        config.app.default_mode.value,
    )
    disabled = [sensor.id for sensor in config.sensors if not sensor.enabled]
    if disabled:
        _log.info("Disabled sensor(s), readings ignored: %s", ", ".join(disabled))
    return config


def _read_yaml(config_path: Path) -> Mapping[str, Any]:
    if not config_path.is_file():
        raise ConfigError(f"Configuration file not found: {config_path}")
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{config_path}: invalid YAML — {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{config_path}: could not be read — {exc}") from exc

    if not isinstance(raw, Mapping):
        raise ConfigError(f"{config_path}: top level must be a mapping of sections")
    return raw


def _describe(exc: ValidationError) -> str:
    """One line per problem, keyed by the path the user has to fix.

    pydantic reports every error at once, so a config with three mistakes needs
    one startup attempt rather than three.
    """
    lines = []
    for error in exc.errors():
        location = _format_location(error["loc"])
        message = error["msg"].removeprefix("Value error, ")
        lines.append(f"{location}: {message}" if location else message)
    return "\n  ".join(lines)


def _format_location(loc: tuple[Any, ...]) -> str:
    """``('sensors', 0, 'x')`` → ``sensors[0].x``."""
    parts: list[str] = []
    for item in loc:
        if isinstance(item, int):
            parts.append(f"[{item}]")
        else:
            parts.append(f".{item}" if parts else str(item))
    return "".join(parts)


def _reject_legacy_keys(raw: Mapping[str, Any]) -> None:
    """Point at the replacement instead of just reporting an unknown key."""
    interpolation = raw.get("interpolation")
    if isinstance(interpolation, Mapping):
        if "model_extent" in interpolation:
            raise ConfigError(
                "interpolation.model_extent has been replaced by the `model` section: "
                "give the model's `length` (X) and `height` (Y) instead, in the same "
                "units as the sensor coordinates."
            )
        if "background_image" in interpolation:
            raise ConfigError(
                "interpolation.background_image has moved to model.background_image"
            )

    sponsor = raw.get("sponsor")
    if isinstance(sponsor, Mapping) and ("logo_path" in sponsor or "link_url" in sponsor):
        raise ConfigError(
            "sponsor.logo_path/link_url have been replaced by a `logos` list, so that "
            "several sponsors can be shown:\n"
            "    sponsor:\n"
            "      logos:\n"
            "        - {path: \"src/assets/sponsor_logo_1.png\", link_url: null}"
        )


def _check_referenced_files(config: Config) -> None:
    """Filesystem checks — kept out of the models, which stay pure data rules."""
    missing: list[str] = []
    if not config.demo.csv_path.is_file():
        missing.append(f"demo.csv_path → {config.demo.csv_path}")
    if config.app.icon_path is not None and not config.app.icon_path.is_file():
        missing.append(f"app.icon_path → {config.app.icon_path}")
    for index, logo in enumerate(config.sponsor.logos):
        if not logo.path.is_file():
            missing.append(f"sponsor.logos[{index}].path → {logo.path}")
    background = config.model.background_image
    if background is not None and not background.is_file():
        missing.append(f"model.background_image → {background}")
    if missing:
        raise ConfigError("Referenced file(s) not found:\n  " + "\n  ".join(missing))


def _warn_about_rates(config: Config) -> None:
    if config.interpolation.redraw_rate_hz > config.app.refresh_rate_hz:
        _log.warning(
            "interpolation.redraw_rate_hz (%.1f) is higher than app.refresh_rate_hz (%.1f); "
            "the heatmap cannot redraw faster than data arrives",
            config.interpolation.redraw_rate_hz,
            config.app.refresh_rate_hz,
        )
