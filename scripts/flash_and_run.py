"""Flash the Arduino sketch for one hardware build, then start the monitor in Live mode.

    uv run scripts/flash_and_run.py --hardware pt100      # 10 × PT100 via MAX31865
    uv run scripts/flash_and_run.py --hardware ds18b20    # 10 × DS18B20

One command for the rig: compile and upload the build's sketch, wait for the board
to come back, then launch the app with `--mode live` on the same port and with the
build's own config file (see :data:`HARDWARE`). Both sketches emit the same serial
protocol, so the app itself never knows which build it is reading — only the config
differs, chiefly in its calibration coefficients.

This is *development tooling*, not part of the application: nothing in `src/`
imports it, and the app never touches `firmware/` at runtime (CLAUDE.md §3.1a).
It does reuse the app's own config and port discovery, so the board it flashes is
by construction the port the app will open.

Requires `arduino-cli` on PATH (or `--arduino-cli`), plus the build's libraries
(`--install-libs` installs them).

Useful variants (all take --hardware):

    ... --fullscreen        # extra args go to the app
    ... --flash-only        # flash, don't launch
    ... --skip-flash        # launch live with the build's config, don't flash
    ... --dry-run           # print the commands only
"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from config.loader import DEFAULT_CONFIG_PATH, load_config  # noqa: E402
from config.schema import ConfigError  # noqa: E402
from datasource.live_serial import resolve_port  # noqa: E402

_log = logging.getLogger("flash_and_run")

DEFAULT_FQBN = "arduino:avr:mega"


@dataclass(frozen=True)
class Hardware:
    """One sensor build of the rig: what to flash and which config reads it."""

    description: str
    sketch: Path
    config: Path
    libraries: tuple[str, ...]


#: --hardware choices. A new build is a sketch folder, a config file and an entry here.
HARDWARE = {
    "pt100": Hardware(
        description="10 x PT100 via MAX31865",
        sketch=PROJECT_ROOT / "firmware" / "pt100_max31865",
        config=DEFAULT_CONFIG_PATH,
        libraries=("Adafruit MAX31865 library",),
    ),
    "ds18b20": Hardware(
        description="10 x DS18B20, one pin each",
        sketch=PROJECT_ROOT / "firmware" / "ds18b20",
        config=PROJECT_ROOT / "config" / "config.ds18b20.yaml",
        libraries=("OneWire", "DallasTemperature"),
    ),
}

#: The board is reset by the upload; give it a moment before the app grabs the port.
PORT_SETTLE_TIMEOUT_S = 10.0


class StepFailed(RuntimeError):
    """A pipeline step failed; the message is already user-facing."""


def parse_args(argv: list[str] | None = None) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(
        prog="flash_and_run",
        description="Flash the Arduino, then run the monitor in Live mode.",
        epilog="Unrecognised arguments are passed through to src/main.py.",
    )
    # Required on purpose: the wrong sketch on a rig reads -999 on every channel,
    # and the wrong config applies another build's calibration.
    parser.add_argument("--hardware", required=True, choices=sorted(HARDWARE),
                        help="sensor build: "
                             + "; ".join(f"{name} = {hw.description}"
                                         for name, hw in HARDWARE.items()))
    parser.add_argument("-c", "--config", type=Path, default=None,
                        help="config file (default: the build's own, see HARDWARE)")
    parser.add_argument("--fqbn", default=DEFAULT_FQBN,
                        help=f"fully qualified board name (default: {DEFAULT_FQBN})")
    parser.add_argument("--sketch", type=Path, default=None,
                        help="sketch directory (default: the build's own, see HARDWARE)")
    parser.add_argument("--port", help="serial port; default is the app's own auto-detection")
    parser.add_argument("--arduino-cli", default=None,
                        help="path to arduino-cli (default: found on PATH)")
    parser.add_argument("--install-libs", action="store_true",
                        help="install the build's Arduino libraries before compiling")
    parser.add_argument("--flash-only", action="store_true", help="flash, then stop")
    parser.add_argument("--skip-flash", action="store_true", help="skip flashing, just run live")
    parser.add_argument("--dry-run", action="store_true", help="print commands, run nothing")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args, passthrough = parser.parse_known_args(argv)

    hardware = HARDWARE[args.hardware]
    args.libraries = hardware.libraries
    if args.sketch is None:
        args.sketch = hardware.sketch
    if args.config is None:
        args.config = hardware.config
    return args, passthrough


def run(command: list[str], *, dry_run: bool, what: str) -> None:
    printable = " ".join(f'"{part}"' if " " in part else part for part in command)
    if dry_run:
        print(f"  [dry-run] {printable}")
        return
    _log.info("%s: %s", what, printable)
    try:
        subprocess.run(command, check=True)
    except FileNotFoundError as exc:
        raise StepFailed(f"{command[0]} not found: {exc}") from exc
    except subprocess.CalledProcessError as exc:
        raise StepFailed(f"{what} failed (exit code {exc.returncode})") from exc


def find_arduino_cli(explicit: str | None, dry_run: bool = False) -> str:
    path = explicit or shutil.which("arduino-cli")
    if not path:
        if dry_run:
            # A dry run is for previewing the pipeline, often on a machine that
            # has no toolchain installed.
            _log.warning("arduino-cli not found; showing the commands anyway")
            return "arduino-cli"
        raise StepFailed(
            "arduino-cli was not found on PATH.\n"
            "  Install it from https://arduino.github.io/arduino-cli/latest/installation/\n"
            "  (Raspberry Pi OS / Debian: the install script provides an arm64 build),\n"
            "  pass --arduino-cli /path/to/arduino-cli, or use --skip-flash to just run "
            "the app."
        )
    return path


def resolve_target_port(config, override: str | None) -> str:
    """Use the app's own discovery so we flash the board it will then read."""
    if override:
        return override
    port = resolve_port(config.serial)
    _log.info("Target port: %s", port)
    return port


def wait_for_port(port: str, timeout: float = PORT_SETTLE_TIMEOUT_S) -> bool:
    """Poll until the port reappears after the upload reset it."""
    from serial.tools import list_ports

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(candidate.device == port for candidate in list_ports.comports()):
            return True
        time.sleep(0.25)
    return False


def flash(args: argparse.Namespace, port: str) -> None:
    cli = find_arduino_cli(args.arduino_cli, args.dry_run)
    sketch = args.sketch.resolve()
    if not sketch.is_dir():
        raise StepFailed(f"Sketch directory not found: {sketch}")

    if args.install_libs:
        for library in args.libraries:
            run([cli, "lib", "install", library], dry_run=args.dry_run,
                what=f"Installing {library}")

    run([cli, "compile", "--fqbn", args.fqbn, str(sketch)],
        dry_run=args.dry_run, what="Compiling sketch")
    run([cli, "upload", "--fqbn", args.fqbn, "-p", port, str(sketch)],
        dry_run=args.dry_run, what="Uploading sketch")

    if args.dry_run:
        return
    if wait_for_port(port):
        # Even once enumerated, the bootloader hands over a moment later.
        time.sleep(1.0)
        _log.info("Board is back on %s", port)
    else:
        _log.warning(
            "%s did not reappear within %.0fs; starting anyway — Live mode retries "
            "on its own", port, PORT_SETTLE_TIMEOUT_S
        )


def launch_app(args: argparse.Namespace, passthrough: list[str]) -> int:
    command = [sys.executable, str(PROJECT_ROOT / "src" / "main.py"),
               "--config", str(args.config), "--mode", "live", *passthrough]
    if args.dry_run:
        print(f"  [dry-run] {' '.join(command)}")
        return 0
    _log.info("Starting the monitor in Live mode")
    return subprocess.run(command).returncode


def main(argv: list[str] | None = None) -> int:
    args, passthrough = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )

    if args.flash_only and args.skip_flash:
        _log.error("--flash-only and --skip-flash are mutually exclusive")
        return 2

    _log.info("Hardware: %s (%s)", args.hardware, HARDWARE[args.hardware].description)
    _log.info("Sketch:   %s", args.sketch)
    _log.info("Config:   %s", args.config)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        # Fail before touching the board: a config that cannot start the app is
        # not worth a flash cycle.
        _log.error("Configuration error: %s", exc)
        return 1

    try:
        port = resolve_target_port(config, args.port)
        if not args.skip_flash:
            flash(args, port)
        if args.flash_only:
            _log.info("Flashed. Skipping the app (--flash-only).")
            return 0
    except StepFailed as exc:
        _log.error("%s", exc)
        return 1

    return launch_app(args, passthrough)


if __name__ == "__main__":
    raise SystemExit(main())
