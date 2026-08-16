# Physical Model Temperature Monitor

A desktop GUI that visualises ten temperature sensors mounted at known positions on a
physical model. An Arduino reads the sensors and streams them over USB to a Raspberry Pi 5,
where this app plots each sensor over time and renders a 2-D interpolated temperature field
across the model. It can also replay a recorded CSV instead of reading the Arduino, so the
whole application can be developed and demonstrated without any hardware attached.

## Screenshot

_TODO: add a screenshot or short GIF of the running app here (`docs/screenshot.png`)._

## Features

- **Live mode** — reads the Arduino over USB serial, with port auto-detection and
  automatic reconnection if the cable is unplugged.
- **Demo mode** — replays a CSV log on loop through the identical pipeline, so nothing
  downstream can tell the difference.
- **Time-series panel** — all sensors on one axis, individually toggleable, with the
  latest value shown next to each sensor and an optional legend (toolbar, or `Ctrl+L`).
- **Temperature field panel** — 2-D interpolation (RBF / linear / cubic / nearest) across
  the model's real geometry, filling the panel with the model area only — no blank
  margins — with sensor markers and an optional model layout image.
- **CSV recording** — live sessions are written to `logs/session_*.csv` in exactly the
  format Demo mode replays.
- **Sponsor branding** — a persistent strip of logos (any number, each optionally
  clickable) docked to whichever window edge you configure; survives any dock
  rearrangement.
- **Themes** — `gray` (system window colours, dark plot canvases) or `white` (white
  chrome and white plots, for bright rooms), plus a configurable window icon.
- **Config-driven** — sensor count, positions, colours, rates, ports and paths all come
  from `config/config.yaml`.

## Hardware setup

- **10 × PT100 RTDs**, each on its own **MAX31865** converter, mounted at the X/Y
  positions listed in `config/config.yaml`.
- **Arduino Mega 2560** — the MAX31865 boards share the hardware SPI bus (D50/D51/D52)
  with one chip-select pin each (D22–D31), streaming one line per sample at 115200 baud,
  1 Hz.
- **Raspberry Pi 5** running Raspberry Pi OS (64-bit), Arduino attached over USB — this is
  the deployment target. Development happens on any PC using Demo mode.

The Arduino sketch lives in [`firmware/`](firmware/README.md) — it is kept in this repo for
reference and is not part of the Python application. See that file for wiring, the sensor
address table and flashing instructions.

## Installation

Dependencies and the interpreter are both managed by [uv](https://docs.astral.sh/uv/).
Python **3.14** is pinned in `.python-version`; uv downloads it if the machine does not
have it, so no manual Python install is needed.

```bash
# install uv (once)
curl -LsSf https://astral.sh/uv/install.sh | sh        # Linux / macOS / Raspberry Pi OS
# Windows: powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

git clone <this repo>
cd turbogrant-twin-sfc
uv sync                                                 # creates .venv from uv.lock
```

`uv sync` is exact and reproducible: it installs the versions recorded in `uv.lock`.
Use `uv add <package>` / `uv remove <package>` to change dependencies — both update
`pyproject.toml` and the lockfile. `uv lock --upgrade` refreshes pinned versions.

### Raspberry Pi notes

Serial access needs `dialout` group membership (log out and back in afterwards):

```bash
sudo usermod -a -G dialout $USER
```

Qt needs a few system libraries that are not in the PySide6 wheel:

```bash
sudo apt install libxcb-cursor0 libxkbcommon-x11-0 libegl1
```

`/dev/ttyACM0` is not a stable name. Either leave `serial.auto_detect: true` (the app scans
for an Arduino-like VID/PID), or pin the device with a udev rule and point `serial.port` at
the symlink:

```
# /etc/udev/rules.d/99-arduino.rules
SUBSYSTEM=="tty", ATTRS{idVendor}=="2341", ATTRS{serial}=="<board serial>", SYMLINK+="arduino_sensors"
```

## Configuration

Everything runtime-configurable lives in [`config/config.yaml`](config/config.yaml).
Relative paths are resolved against the project root. The sensor **count is not hardcoded** —
add or remove entries under `sensors:` and the plots, interpolation and CSV columns follow.

`app.refresh_rate_hz` controls how smoothly the time-series plot scrolls — the axis
follows wall-clock time, so higher is smoother and costs proportionally more CPU. 10 Hz
suits a desktop; on the Raspberry Pi start around 5 Hz and raise it while watching `top`.
It is unrelated to how fast data arrives. The field panel is only redrawn when a new
sample arrives, so `interpolation.redraw_rate_hz` caps latency rather than CPU.

The model's size is set once under `model:` as `length` (X) and `height` (Y), in the same
units as the sensor coordinates — centimetres by default. The origin is the bottom-left
corner, so every sensor must sit within `0..length` by `0..height`; startup fails with a
clear message otherwise. The field panel draws exactly that rectangle and nothing else.
Set `keep_aspect: true` if true proportions matter more than filling the panel — the model
is then letterboxed rather than stretched.

```yaml
app:
  default_mode: demo            # live | demo
  refresh_rate_hz: 10           # time-series scroll smoothness
  fullscreen: false
  theme: gray                   # gray (system chrome, dark plots) | white
  icon_path: "src/assets/app_icon.png"   # window/taskbar icon; null for Qt's default

serial:
  port: "/dev/ttyACM0"
  baud_rate: 115200
  auto_detect: true             # scan for an Arduino VID/PID before using `port`

demo:
  csv_path: "data/demo/sample_run.csv"
  playback_rate_hz: 2
  loop: true

model:
  length: 50.0                  # model size along X, in `units`
  height: 30.0                  # model size along Y
  units: "cm"
  keep_aspect: false            # false: field fills the panel, no blank margins
  background_image: null        # optional layout drawn under the field

sensors:
  - {id: T1, label: "Bottom left", x: 24.0, y: 8.0}
  # ... one entry per sensor; x/y are positions on the model, in cm.
  # The default is a staggered 3/4/3 grid covering the whole model.

interpolation:
  method: "rbf"                 # rbf | linear | cubic | nearest
  grid_resolution: 100          # keep modest on the Pi

plotting:
  time_window_s: 300
  temperature_units: "C"        # display only — data is stored in Celsius
  show_legend: true             # legend at startup; toggle with Ctrl+L
  curve_width: 1                # px; 1 draws about twice as fast as wider pens

sponsor:
  position: bottom              # bottom | top | left | right
  alignment: center             # left | center | right along the bar
  max_height_px: 60
  logos:
    - {path: "src/assets/sponsor_logo_1.png", link_url: null}
    - {path: "src/assets/sponsor_logo_2.png", link_url: null}
    - {path: "src/assets/sponsor_logo_3.png", link_url: null}
```

Configuration is validated at startup by pydantic models: duplicate sensor ids, positions
outside the model, mistyped key names, missing files or bad values stop the app with a
dialog listing every problem, each keyed by its exact path (e.g. `sensors[2].x`).

## Running the app

```bash
uv run python src/main.py                 # uses app.default_mode from the config
uv run python src/main.py --mode demo     # force Demo mode (no hardware needed)
uv run python src/main.py --mode live     # force Live mode
uv run python src/main.py --fullscreen    # kiosk/exhibit rig
uv run python src/main.py -c /path/to/other.yaml --log-level DEBUG
```

`uv run` syncs the environment before starting, so a fresh clone needs nothing else.
(Activating `.venv` and running `python src/main.py` directly also works.)

### Flash the Arduino and run, in one command

On the rig, `scripts/flash_and_run.py` compiles and uploads
[`firmware/sensor_reader`](firmware/README.md), waits for the board to re-enumerate,
then starts the app in Live mode on the same port:

```bash
uv run scripts/flash_and_run.py                 # flash, then run live
uv run scripts/flash_and_run.py --install-libs  # also install the MAX31865 library
uv run scripts/flash_and_run.py --flash-only    # flash and stop
uv run scripts/flash_and_run.py --skip-flash    # just run live
uv run scripts/flash_and_run.py --dry-run       # print the commands, run nothing
```

It needs [`arduino-cli`](https://arduino.github.io/arduino-cli/latest/installation/)
on PATH (or `--arduino-cli /path/to/it`). The board is selected with `--fqbn`
(default `arduino:avr:mega`) and the port comes from the app's own auto-detection, so
the board that gets flashed is the one the app then reads — override with `--port`.
Unrecognised arguments are passed through to the app, e.g.
`uv run scripts/flash_and_run.py --fullscreen`.

The config is validated *before* the board is touched, so a broken `config.yaml`
cannot cost you a flash cycle.

Mode can also be switched at runtime from the toolbar, along with Start/Stop, Pause and
Restart replay (Demo), and CSV recording.

## Arduino serial protocol

One `\n`-terminated line per sample, at `serial.baud_rate`:

```
T1:23.44,T2:23.06,T3:22.91,T4:23.88,T5:25.10,T6:24.02,T7:23.55,T8:24.71,T9:23.19,T10:21.80
```

`T` + 1–2 digits, a colon, then a signed integer or decimal; pairs separated by commas
(spaces around them are tolerated). Lines that do not match, or that do not cover every
configured sensor, are discarded and logged rather than plotted.

**The format is configurable** — match the sketch instead of editing Python, under
`serial.protocol` in `config/config.yaml`:

```yaml
serial:
  protocol:
    format: key_value           # key_value | values | regex
    field_separator: ","        # e.g. ";" 
    key_value_separator: ":"    # e.g. "="
    allow_whitespace: true      # "T1:23.44, T2:23.06" parses too
    line_prefix: null           # framing stripped first, e.g. "DATA:"
    line_suffix: null
    pattern: null               # format: regex — named groups are sensor ids
```

- `key_value` — `T1:23.44,T2:23.06,…` (default), order-independent.
- `values` — `23.44,23.06,…`, mapped positionally onto the `sensors` list.
- `regex` — anything else, e.g. `"^#(?P<T1>-?\\d+\\.\\d+);(?P<T2>-?\\d+\\.\\d+)$"`.

Invalid combinations (a regex whose groups don't match the sensor ids, an `id_pattern`
that can't match them, identical separators) fail at startup rather than silently
discarding every line. Parsing lives in one place — `src/datasource/protocol.py`.

A faulted channel (open or shorted RTD) is sent as `-999.00`: the frame stays complete, and
the value falls outside `sensor_limits.value_range`, so the app flags that sensor in red.

Recorded CSVs mirror this: a `timestamp` column plus one column per sensor id, so any
session in `logs/` can be pointed at by `demo.csv_path` and replayed.

## Project structure

```
scripts/flash_and_run.py  dev helper: flash the Arduino, then run in Live mode
pyproject.toml          dependencies and Python requirement (uv-managed)
uv.lock                 exact resolved versions — commit it, do not edit by hand
.python-version         interpreter pin (3.14) used by uv
config/config.yaml      runtime configuration (the only place settings live)
data/demo/              example CSV for Demo mode
firmware/               Arduino sketch + wiring notes (not part of the Python app)
src/main.py             entry point
src/config/             YAML loading, schema and validation
src/datasource/         DataSource interface + live serial and CSV replay backends
src/processing/         rolling buffer, interpolation, units (no Qt imports)
src/gui/                main window, mode controller, dockable panels
src/logging_utils/      app logging and CSV session recording
```

Live and Demo sources emit identical `SensorFrame` objects, so no panel, buffer or logger
knows where data came from. New panels are added as files under `src/gui/panels/` and
registered in `MainWindow`.

## Planned

- Screenshot/GIF in this README once the GUI is running on the rig.
- Optional model layout image behind the heatmap (`interpolation.background_image` is
  already supported; the image itself does not exist yet).
- Autostart on the Pi via a `systemd` user service for permanent exhibit use.

## Sponsor

_TODO: sponsor acknowledgement._

The in-app branding strip is configured under `sponsor:` in `config/config.yaml`:
`position` picks the window edge (bottom/top/left/right), `alignment` the placement
along it, and `logos:` lists the artwork — add or remove entries freely, and give any
logo its own `link_url` or `max_height_px`. `src/assets/sponsor_logo_{1,2,3}.png` are
placeholders; replace them with the real artwork (PNG with transparency works best).

## License

GPL-3.0 — see [LICENSE](LICENSE).
