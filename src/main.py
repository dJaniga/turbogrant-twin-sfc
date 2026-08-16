"""Entry point: parse arguments, load config, show the window.

Run with ``python src/main.py`` (see README). ``src/`` is the import root, so the
packages below are imported as ``config``, ``datasource``, ``gui``, ... .
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Allow both `python src/main.py` and `python -m main` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from config.loader import DEFAULT_CONFIG_PATH, load_config  # noqa: E402
from config.schema import AppConfig, Config, ConfigError, LoggingConfig, Mode  # noqa: E402
from gui.main_window import MainWindow  # noqa: E402
from logging_utils.logger import configure_logging  # noqa: E402

_log = logging.getLogger(__name__)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="sensor-monitor",
        description="Live/demo temperature monitor for a sensor-instrumented physical model.",
    )
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"path to the YAML config file (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "-m",
        "--mode",
        choices=[mode.value for mode in Mode],
        help="override app.default_mode from the config file",
    )
    display = parser.add_mutually_exclusive_group()
    display.add_argument(
        "--fullscreen", action="store_true", help="start full screen (kiosk/exhibit rig)"
    )
    display.add_argument(
        "--windowed", action="store_true", help="start windowed even if the config says fullscreen"
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="override logging.level from the config file",
    )
    return parser.parse_args(argv)


def apply_overrides(config: Config, args: argparse.Namespace) -> Config:
    """CLI flags win over the file; the file wins over defaults."""
    app_overrides: dict[str, object] = {}
    if args.mode:
        app_overrides["default_mode"] = Mode(args.mode)
    if args.fullscreen:
        app_overrides["fullscreen"] = True
    if args.windowed:
        app_overrides["fullscreen"] = False

    # The models are frozen, so changed sections are rebuilt *through validation*
    # — a CLI flag cannot smuggle in a value the config file would have rejected.
    overrides: dict[str, object] = {}
    if app_overrides:
        overrides["app"] = AppConfig.model_validate(
            {**config.app.model_dump(), **app_overrides}
        )
    if args.log_level:
        overrides["logging"] = LoggingConfig.model_validate(
            {**config.logging.model_dump(), "level": args.log_level}
        )
    return config.model_copy(update=overrides) if overrides else config


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # QApplication first, so a config error can be shown in a dialog rather than
    # only on a console the user may not be looking at (CLAUDE.md §4/§6).
    app = QApplication(sys.argv[:1])

    try:
        config = apply_overrides(load_config(args.config), args)
    except ConfigError as exc:
        logging.basicConfig(level=logging.INFO)
        _log.error("Configuration error: %s", exc)
        QMessageBox.critical(
            None,
            "Configuration error",
            f"{exc}\n\nFile: {args.config}\n\nFix the file and start the app again.",
        )
        return 1

    configure_logging(config.logging)
    _log.info("Starting %s", config.app.title)

    app.setApplicationName(config.app.title)
    app.setOrganizationName("Sensor Monitor")

    window = MainWindow(config)
    window.show()
    window.start()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
