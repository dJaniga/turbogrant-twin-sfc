# Firmware

The Arduino sketches that feed the monitor. They are **not part of the Python
application** — nothing in `src/` imports, builds or runs them. They live here so the
hardware and software sides of the project stay in one repository and in sync,
because the serial line format below is a contract between these sketches and
`src/datasource/protocol.py`.

The rig exists in two sensor builds. Both run on an Arduino Mega 2560 and emit
**exactly the same serial protocol**, so the app does not know or care which one is
attached — only the config file differs (each build has its own calibration):

| Build | Sketch | Config | `--hardware` |
|-------|--------|--------|--------------|
| 10 × PT100 via 10 × MAX31865 (SPI) | [`pt100_max31865/`](pt100_max31865/pt100_max31865.ino) | `config/config.yaml` | `pt100` |
| 10 × DS18B20, one OneWire pin each | [`ds18b20/`](ds18b20/ds18b20.ino) | `config/config.ds18b20.yaml` | `ds18b20` |

> ⚠️ Both sketches are **reference implementations**, written to match the protocol
> the Python app expects. They have not been run against the real rig. If a deployed
> sketch differs, treat that one as the source of truth and update `serial.protocol`
> in the config and this file to match.

## Serial output format (both builds)

One line per sample, `\n`-terminated, 115200 baud:

```
T1:23.44,T2:23.06,T3:22.91,T4:23.88,T5:25.10,T6:24.02,T7:23.55,T8:24.71,T9:23.19,T10:21.80
```

Rules the Python parser enforces, as configured by `serial.protocol` in the config
(parser: `src/datasource/protocol.py`):

- `T` followed by 1–2 digits, a colon, then a signed integer or decimal.
- Pairs separated by `,`; spaces around the separators are tolerated, a trailing
  separator is not.
- No `NaN`, `inf` or scientific notation — malformed lines are discarded and logged.
- Every enabled sensor must be present in the line; partial frames are dropped.
  Sensors with `enabled: false` in the config are ignored whether present or not.

**If the output changes, change the config, not the Python** — and change *both*
sketches, so the builds stay interchangeable. Different separators, bare CSV without
ids, a `DATA:` prefix or a trailing marker are all expressible under
`serial.protocol` (`format: key_value | values | regex`). Update this section and
CLAUDE.md §5.1 to match whatever the deployed sketches actually emit.

### Faults

A channel that cannot be read is sent as `-999.00`. That keeps the frame complete
(the app drops frames that miss an enabled sensor), while sitting far outside
`sensor_limits.value_range`, so the GUI flags the sensor in red instead of plotting a
plausible-looking wrong value. What counts as a fault is build-specific, see below.

Never print diagnostics on the data serial line — anything that does not match the
protocol is discarded by the parser and reported as a malformed line.

### No offsets in firmware

The residual spread between channels is corrected in software, under `calibration:`
in the build's config file (fitted by `scripts/calibrate.py -c <config>`). Correcting
in both places would double-count. Hardware constants (`RREF`, `RNOMINAL`,
resolution) are the exception — they describe the hardware, not the sensor's error.

## Flashing

From the project root, one command flashes the board and starts the app in Live mode
on the same port, with the matching config:

```bash
uv run scripts/flash_and_run.py --hardware pt100   --install-libs   # --install-libs only needed once
uv run scripts/flash_and_run.py --hardware ds18b20 --install-libs
```

`--hardware` is required: the wrong sketch on a rig reads `-999.00` everywhere, and
the wrong config applies another build's calibration. Add `--dry-run` to see the
commands, `--flash-only` / `--skip-flash` to do one half.

Arduino IDE: open the build's `.ino`, select *Arduino Mega or Mega 2560*, pick the
port, upload. Or with `arduino-cli` directly:

```bash
arduino-cli lib install "Adafruit MAX31865 library"          # PT100 build
arduino-cli lib install OneWire DallasTemperature            # DS18B20 build
arduino-cli compile --fqbn arduino:avr:mega firmware/pt100_max31865
arduino-cli upload  --fqbn arduino:avr:mega -p /dev/ttyACM0 firmware/pt100_max31865
```

Close the serial monitor (and the monitor app) before uploading — the port cannot
be open twice.

---

## PT100 build — `pt100_max31865/`

| Item | Value |
|------|-------|
| Board | Arduino Mega 2560 |
| Sensors | 10 × PT100 RTD |
| Signal conditioning | 10 × MAX31865 RTD-to-digital converter (SPI) |
| Library | `Adafruit_MAX31865` (Arduino Library Manager) |
| Sample rate | 1 Hz (`SAMPLE_INTERVAL_MS = 1000`) — see *Timing* below |
| RTD wiring mode | 4-wire (`MAX31865_4WIRE`) |
| Reference resistor | 430 Ω (`RREF`), nominal 100 Ω at 0 °C (`RNOMINAL`) |

The Mega 2560 is used because ten MAX31865 boards need ten chip-select pins on top
of the SPI bus, which an Uno/Nano cannot comfortably provide.

### Wiring

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
| T1 | D30 | T6  | D35 |
| T2 | D31 | T7  | D36 |
| T3 | D32 | T8  | D37 |
| T4 | D33 | T9  | D38 |
| T5 | D34 | T10 | D39 |

D53 is the Mega's hardware SS pin. Nothing connects to it, but the sketch drives it
high: if it is left as an input and gets pulled low, the AVR's SPI peripheral drops
out of master mode.

### PT100 side

- **Wiring mode must match the board.** The sketch uses `MAX31865_4WIRE`
  (`RTD_WIRES`). Adafruit's breakout ships configured for 2/4-wire; for 3-wire PT100s
  cut the 2/4-wire jumper, bridge the 3-wire pads *and* set `MAX31865_3WIRE` — the
  sketch setting alone is not enough.
- **`RREF` must match the board.** Adafruit breakouts ship with 430 Ω for PT100
  (4300 Ω on PT1000 versions). Measure yours and put the real value in the sketch:
  it scales every reading linearly.
- If you switch to PT1000, set `RNOMINAL = 1000.0` and `RREF = 4300.0`.
- Sensor → position mapping is fixed by the `CS_PINS[]` order (T1 … T10); the X/Y
  position of each `T<n>` lives in the config on the Python side.

### Timing

`Adafruit_MAX31865::temperature()` performs a **one-shot** conversion: ~10 ms bias
settling plus ~65 ms conversion, so ten sensors read sequentially cost ~750 ms.
That is why the sketch samples at 1 Hz rather than the 2 Hz the demo data uses.

To go faster, put the amplifiers in continuous-conversion mode (`autoConvert(true)`
with the bias left on) and read the RTD registers directly, without the per-read
delays. The library keeps its raw register reads private, so that needs a small
custom SPI driver in this sketch. The Python app is rate-agnostic either way — it
timestamps frames on arrival and shows the measured rate in the status bar.

### Faults

`readFault()` reports open circuits, shorts and over/under-voltage. When a channel
faults, the sketch clears the fault and sends `-999.00` for that sensor.

---

## DS18B20 build — `ds18b20/`

| Item | Value |
|------|-------|
| Board | Arduino Mega 2560 |
| Sensors | 10 × DS18B20 digital thermometer (OneWire) |
| Libraries | `OneWire`, `DallasTemperature` (Arduino Library Manager) |
| Sample rate | 1 Hz (`SAMPLE_INTERVAL_MS = 1000`) |
| Resolution | 12-bit, 0.0625 °C (`RESOLUTION_BITS`) |
| Range | −55 … 125 °C (`sensor_limits.value_range` in `config.ds18b20.yaml`) |

### Wiring

One sensor per pin, each pin its own OneWire bus. The pin fixes which `T<n>` a
sensor is, so no ROM addresses are needed and a replaced probe needs no sketch change.

```
DS18B20 VDD (red)    ── 5 V                       (all sensors)
DS18B20 GND (black)  ── GND                       (all sensors)
DS18B20 DQ  (yellow) ── its own pin, see table, + 4.7 kΩ pull-up to 5 V per pin
```

| Sensor | Pin | Sensor | Pin |
|--------|-----|--------|-----|
| T1 | D22 | T6  | D27 |
| T2 | D23 | T7  | D28 |
| T3 | D24 | T8  | D29 |
| T4 | D25 | T9  | D30 |
| T5 | D26 | T10 | D31 |

- **Every DQ line needs its own pull-up** (4.7 kΩ to 5 V). On long cable runs
  (several metres) drop it to ~2.2 kΩ if readings drop out.
- Power the sensors from VDD, not parasite power — the sketch assumes normal power.
- Pins are set in the `buses[]` initialiser; any free digital pins will do.

### Timing

A 12-bit conversion takes up to 750 ms, but the sketch starts the conversion on all
ten buses at once, waits once (`CONVERSION_MS`), then reads them all — so a whole
frame takes ~750 ms and 1 Hz is comfortable. The loop never blocks while waiting.

### Faults

`-999.00` is sent when:

- no sensor answers on the pin (missing or unplugged probe);
- `getTempC()` returns `DEVICE_DISCONNECTED_C` (−127), which also covers a CRC error;
- the reading is exactly `85.00` °C — the DS18B20's power-on value, meaning the
  conversion never ran (typically a supply glitch).

A missing sensor is searched for again on every cycle, so re-plugging a probe
recovers within a second without resetting the board.
