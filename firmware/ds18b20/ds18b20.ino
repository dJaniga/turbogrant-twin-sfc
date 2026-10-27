/*
 * ds18b20 — DS18B20 build of the Physical Model Temperature Monitor sketch.
 *
 * Reads 10 DS18B20 digital sensors, one per OneWire pin, and prints one line per
 * sample over USB — exactly the same line as the PT100 build:
 *
 *     T1:23.44,T2:23.06,...,T10:24.00\n
 *
 * This format is a contract with the Python app (CLAUDE.md §5.1, parsed per
 * `serial.protocol` in config/config.ds18b20.yaml by src/datasource/protocol.py)
 * and is shared with firmware/pt100_max31865 — change one, change both, and update
 * firmware/README.md. Flash with:
 *
 *     uv run scripts/flash_and_run.py --hardware ds18b20
 *
 * Board:    Arduino Mega 2560
 * Library:  OneWire, DallasTemperature (Arduino Library Manager)
 * Wiring:   One sensor per pin, D22..D31 for T1..T10, each DQ line with its own
 *           4.7 kΩ pull-up to 5 V. Sensors powered from VDD (not parasite power).
 *           See firmware/README.md.
 */

#include <OneWire.h>
#include <DallasTemperature.h>

static const uint8_t SENSOR_COUNT = 10;
static const unsigned long BAUD_RATE = 115200;

/*
 * Sample period. A 12-bit DS18B20 conversion takes up to 750 ms, but every bus
 * converts at the same time: the sketch starts all ten conversions, waits once,
 * then reads them all. 1 Hz matches the PT100 build; the Python app does not care
 * either way — it timestamps on arrival and the status bar shows the measured Hz.
 */
static const unsigned long SAMPLE_INTERVAL_MS = 1000;

/* Datasheet maximum conversion time at 12-bit resolution. */
static const unsigned long CONVERSION_MS = 750;
static const uint8_t RESOLUTION_BITS = 12;

/*
 * Sent when a sensor is missing, fails its CRC, or returns the power-on value.
 * Same sentinel as the PT100 build: it keeps frames complete while sitting far
 * outside `sensor_limits.value_range`, so the app flags that sensor in red.
 */
static const float FAULT_VALUE = -999.0f;

/* 85.00 °C is the scratchpad's power-on reset value: a conversion that never ran. */
static const float POWER_ON_VALUE = 85.0f;

/*
 * One OneWire bus per sensor, T1..T10 on D22..D31. The pin fixes which T<n> a
 * sensor is — no ROM addresses to discover or hardcode, and a replaced probe needs
 * no sketch change. Any free digital pins will do.
 */
static OneWire buses[SENSOR_COUNT] = {
    OneWire(22), OneWire(23), OneWire(24), OneWire(25), OneWire(26),
    OneWire(27), OneWire(28), OneWire(29), OneWire(30), OneWire(31),
};
static DallasTemperature sensors[SENSOR_COUNT];
static DeviceAddress addresses[SENSOR_COUNT];
static bool present[SENSOR_COUNT];

static bool converting = false;
static unsigned long nextSampleAt = 0;
static unsigned long conversionDoneAt = 0;

/* Look for the sensor on bus `i`. Retried every cycle while it is missing. */
static void connectSensor(uint8_t i) {
  sensors[i].begin();
  present[i] = sensors[i].getAddress(addresses[i], 0);
  if (present[i]) {
    sensors[i].setResolution(addresses[i], RESOLUTION_BITS);
    /* Return from requestTemperatures() at once; the loop does the waiting. */
    sensors[i].setWaitForConversion(false);
  }
}

void setup() {
  Serial.begin(BAUD_RATE);
  for (uint8_t i = 0; i < SENSOR_COUNT; i++) {
    sensors[i].setOneWire(&buses[i]);
    connectSensor(i);
  }
}

static void startConversions() {
  for (uint8_t i = 0; i < SENSOR_COUNT; i++) {
    if (!present[i]) {
      connectSensor(i);  // a re-plugged probe recovers without a reset
    }
    if (present[i]) {
      sensors[i].requestTemperatures();
    }
  }
}

static float readSensor(uint8_t i) {
  if (!present[i]) {
    return FAULT_VALUE;
  }
  const float celsius = sensors[i].getTempC(addresses[i]);
  if (celsius == DEVICE_DISCONNECTED_C) {
    /* Unplugged, or a bad CRC. Search the bus again next cycle. */
    present[i] = false;
    return FAULT_VALUE;
  }
  if (celsius == POWER_ON_VALUE) {
    return FAULT_VALUE;
  }
  return celsius;
}

void loop() {
  const unsigned long now = millis();

  if (!converting) {
    if ((long)(now - nextSampleAt) < 0) {
      return;
    }
    nextSampleAt = now + SAMPLE_INTERVAL_MS;
    startConversions();
    conversionDoneAt = millis() + CONVERSION_MS;
    converting = true;
    return;
  }

  if ((long)(now - conversionDoneAt) < 0) {
    return;
  }
  converting = false;

  for (uint8_t i = 0; i < SENSOR_COUNT; i++) {
    Serial.print('T');
    Serial.print(i + 1);
    Serial.print(':');
    Serial.print(readSensor(i), 2);  // no exponent, no NaN — see CLAUDE.md §5.1
    if (i + 1 < SENSOR_COUNT) {
      Serial.print(',');
    }
  }
  Serial.print('\n');
}
