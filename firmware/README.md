# Firmware

The Arduino sketch that feeds the monitor. It is **not part of the Python
application** — nothing in `src/` imports, builds or runs it. It lives here so the
hardware and software sides of the project stay in one repository and in sync,
because the serial line format below is a contract between this sketch and
`src/datasource/live_serial.py`.

> ⚠️ `sensor_reader/sensor_reader.ino` is a **reference implementation**, written to
> match the protocol the Python app expects. It has not been run against the real
> rig. If the deployed sketch differs, treat that one as the source of truth and
> update both this file and `LINE_RE`/`FIELD_RE` in
> `src/datasource/live_serial.py`.

## Board and libraries

| Item | Value |
|------|-------|
| Board | Arduino Mega 2560 |
| Sensors | 10 × PT100 RTD |
| Signal conditioning | 10 × MAX31865 RTD-to-digital converter (SPI) |
| Library | `Adafruit_MAX31865` (Arduino Library Manager) |
| Baud rate | 115200 |
| Sample rate | 1 Hz (`SAMPLE_INTERVAL_MS = 1000`) — see *Timing* below |
| RTD wiring mode | 3-wire (`MAX31865_3WIRE`) |
| Reference resistor | 430 Ω (`RREF`), nominal 100 Ω at 0 °C (`RNOMINAL`) |

The Mega 2560 is used because ten MAX31865 boards need ten chip-select pins on top
of the SPI bus, which an Uno/Nano cannot comfortably provide.

## Wiring

All ten amplifiers share the hardware SPI bus and differ only in chip select:

```
MAX31865 VIN ── 5 V            (all boards)
MAX31865 GND ── GND            (all boards)
MAX31865 SCK ── D52  (SCK)     (all boards)
MAX31865 SDO ── D50  (MISO)    (all boards)
MAX31865 SDI ── D51  (MOSI)    (all boards)
MAX31865 CS  ── one pin each, see table
```

| Sensor | CS pin | Sensor | CS pin |
|--------|--------|--------|--------|
| T1 | D22 | T6  | D27 |
| T2 | D23 | T7  | D28 |
| T3 | D24 | T8  | D29 |
| T4 | D25 | T9  | D30 |
| T5 | D26 | T10 | D31 |

D53 is the Mega's hardware SS pin. Nothing connects to it, but the sketch drives it
high: if it is left as an input and gets pulled low, the AVR's SPI peripheral drops
out of master mode.

### PT100 side

- **3-wire PT100**: two leads of the same colour go to the board's `RTD+`/`F+`
  terminals per the breakout's silkscreen, the third to `RTD-`. On Adafruit's
  MAX31865 breakout, 3-wire mode also requires cutting the 2/4-wire jumper and
  bridging the 3-wire pads — the sketch's `MAX31865_3WIRE` setting alone is not
  enough, the board must be configured to match.
- **`RREF` must match the board.** Adafruit breakouts ship with 430 Ω for PT100
  (4300 Ω on PT1000 versions). Measure yours and put the real value in the sketch:
  it scales every reading linearly.
- If you switch to PT1000, set `RNOMINAL = 1000.0` and `RREF = 4300.0`.
- Sensor → position mapping is fixed by the `CS_PINS[]` order (T1 … T10); the X/Y
  position of each `T<n>` lives in `config/config.yaml` on the Python side.

## Timing

`Adafruit_MAX31865::temperature()` performs a **one-shot** conversion: ~10 ms bias
settling plus ~65 ms conversion, so ten sensors read sequentially cost ~750 ms.
That is why the sketch samples at 1 Hz rather than the 2 Hz the demo data uses.

To go faster, put the amplifiers in continuous-conversion mode (`autoConvert(true)`
with the bias left on) and read the RTD registers directly, without the per-read
delays. The library keeps its raw register reads private, so that needs a small
custom SPI driver in this sketch. The Python app is rate-agnostic either way — it
timestamps frames on arrival and shows the measured rate in the status bar.

## Fault handling

`readFault()` reports open circuits, shorts and over/under-voltage. When a channel
faults, the sketch clears the fault and sends `-999.00` for that sensor. That keeps
the frame complete (the app drops frames that do not cover every sensor), while
sitting far outside `sensor_limits.value_range` in `config/config.yaml`, so the GUI
flags the sensor in red instead of plotting a plausible-looking wrong value.

Never print diagnostics on the data serial line — anything that does not match the
protocol is discarded by the parser and reported as a malformed line.

## Serial output format

One line per sample, `\n`-terminated:

```
T1:23.44,T2:23.06,T3:22.91,T4:23.88,T5:25.10,T6:24.02,T7:23.55,T8:24.71,T9:23.19,T10:21.80
```

Rules the Python parser enforces, as configured by `serial.protocol` in
`config/config.yaml` (parser: `src/datasource/protocol.py`):

- `T` followed by 1–2 digits, a colon, then a signed integer or decimal.
- Pairs separated by `,`; spaces around the separators are tolerated, a trailing
  separator is not.
- No `NaN`, `inf` or scientific notation — malformed lines are discarded and logged.
- Every configured sensor must be present in the line; partial frames are dropped.

**If this sketch's output changes, change the config, not the Python.** Different
separators, bare CSV without ids, a `DATA:` prefix or a trailing marker are all
expressible under `serial.protocol` (`format: key_value | values | regex`). Update
this section and CLAUDE.md §5.1 to match whatever the deployed sketch actually emits.

## Flashing

From the project root, one command flashes the board and starts the app in Live mode
on the same port:

```bash
uv run scripts/flash_and_run.py --install-libs     # --install-libs only needed once
```

Arduino IDE: open `sensor_reader/sensor_reader.ino`, select *Arduino Mega or Mega
2560*, pick the port, upload. Or with `arduino-cli` directly:

```bash
arduino-cli lib install "Adafruit MAX31865 library"
arduino-cli compile --fqbn arduino:avr:mega firmware/sensor_reader
arduino-cli upload  --fqbn arduino:avr:mega -p /dev/ttyACM0 firmware/sensor_reader
```

Close the serial monitor (and the monitor app) before uploading — the port cannot
be open twice.
