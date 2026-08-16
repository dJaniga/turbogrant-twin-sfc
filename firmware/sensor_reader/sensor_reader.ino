/*
 * sensor_reader — reference sketch for the Physical Model Temperature Monitor.
 *
 * Reads 10 PT100 RTDs through 10 MAX31865 amplifiers on the Mega 2560's hardware
 * SPI bus (one chip-select pin each) and prints one line per sample over USB:
 *
 *     T1:23.44,T2:23.06,...,T10:24.00\n
 *
 * This format is a contract with the Python app: see CLAUDE.md §5.1 and
 * src/datasource/live_serial.py (FIELD_RE / LINE_RE). Change one, change both,
 * and update firmware/README.md.
 *
 * Board:    Arduino Mega 2560
 * Library:  Adafruit_MAX31865 (Arduino Library Manager)
 * Wiring:   SPI bus shared — SCK D52, MISO(SDO) D50, MOSI(SDI) D51.
 *           One CS pin per amplifier, D22..D31 for T1..T10.
 *           See firmware/README.md for the RTD side and the 3-wire jumper.
 */

#include <SPI.h>
#include <Adafruit_MAX31865.h>

static const uint8_t SENSOR_COUNT = 10;
static const unsigned long BAUD_RATE = 115200;

/*
 * Sample period. Budget: Adafruit_MAX31865::temperature() runs a one-shot
 * conversion — bias settle (10 ms) + conversion (65 ms) ≈ 75 ms per sensor, so
 * ten sensors take ~750 ms. 1 Hz is therefore the safe ceiling for a sequential
 * one-shot loop. For 2 Hz, switch the amplifiers to continuous-conversion mode
 * (autoConvert) and read the RTD registers without the per-read delays — that
 * needs a small custom driver, since the library's raw reads are private.
 *
 * The Python app does not care about the rate: it timestamps on arrival and the
 * status bar shows the measured Hz.
 */
static const unsigned long SAMPLE_INTERVAL_MS = 1000;

/* Chip-select pin per sensor, T1..T10. Any free digital pins will do. */
static const uint8_t CS_PINS[SENSOR_COUNT] = {22, 23, 24, 25, 26, 27, 28, 29, 30, 31};

/*
 * Reference resistor on the MAX31865 breakout and the RTD's nominal resistance
 * at 0 °C. Adafruit boards ship with 430 Ω for PT100 (4300 Ω / PT1000).
 * Measure the actual RREF and put the real value here — it scales every reading.
 */
static const float RREF = 430.0f;
static const float RNOMINAL = 100.0f;

/* PT100 wiring per channel: MAX31865_2WIRE, MAX31865_3WIRE or MAX31865_4WIRE. */
static const max31865_numwires_t RTD_WIRES = MAX31865_3WIRE;

/*
 * Sent when an amplifier reports a fault (open/short RTD, broken cable). It is a
 * valid value on the wire, so frames stay complete, but it sits far outside
 * `sensor_limits.value_range` in config.yaml, so the app flags that sensor in
 * red instead of plotting a plausible-looking lie.
 */
static const float FAULT_VALUE = -999.0f;

/* Hardware SPI: the constructor takes only the CS pin. */
static Adafruit_MAX31865 rtd[SENSOR_COUNT] = {
    Adafruit_MAX31865(22), Adafruit_MAX31865(23), Adafruit_MAX31865(24),
    Adafruit_MAX31865(25), Adafruit_MAX31865(26), Adafruit_MAX31865(27),
    Adafruit_MAX31865(28), Adafruit_MAX31865(29), Adafruit_MAX31865(30),
    Adafruit_MAX31865(31),
};

static unsigned long nextSampleAt = 0;

void setup() {
  Serial.begin(BAUD_RATE);

  /*
   * The AVR SPI peripheral drops out of master mode if its hardware SS pin (D53
   * on the Mega) is an input and gets pulled low. Nothing is connected to it,
   * but drive it high anyway.
   */
  pinMode(53, OUTPUT);
  digitalWrite(53, HIGH);

  /*
   * Park every chip select high before talking to any of them: a floating CS on
   * a shared bus lets two amplifiers drive MISO at once.
   */
  for (uint8_t i = 0; i < SENSOR_COUNT; i++) {
    pinMode(CS_PINS[i], OUTPUT);
    digitalWrite(CS_PINS[i], HIGH);
  }

  for (uint8_t i = 0; i < SENSOR_COUNT; i++) {
    rtd[i].begin(RTD_WIRES);
  }
}

void loop() {
  const unsigned long now = millis();
  if ((long)(now - nextSampleAt) < 0) {
    return;
  }
  nextSampleAt = now + SAMPLE_INTERVAL_MS;

  for (uint8_t i = 0; i < SENSOR_COUNT; i++) {
    float celsius = rtd[i].temperature(RNOMINAL, RREF);

    /* temperature() clears the fault register on entry, so check it after. */
    const uint8_t fault = rtd[i].readFault();
    if (fault || isnan(celsius)) {
      rtd[i].clearFault();
      celsius = FAULT_VALUE;
    }

    Serial.print('T');
    Serial.print(i + 1);
    Serial.print(':');
    Serial.print(celsius, 2);  // no exponent, no NaN — see LINE_RE
    if (i + 1 < SENSOR_COUNT) {
      Serial.print(',');
    }
  }
  Serial.print('\n');
}
