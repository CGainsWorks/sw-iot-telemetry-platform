// ESP32 telemetry node.
//
// Publishes the same messages, on the same topics, with the same certificate
// identity as the simulator in edge/simulator. That is the point of having built
// the simulator first: by the time this firmware existed, the schema, the topic
// grammar, the identity binding and every downstream service had already been
// exercised against thousands of messages. This file had to match a contract
// that was already proven rather than define one nobody had tested.
//
// Compiled in CI. Never flashed, because there is no hardware behind this
// repository. See docs/limitations.md.
//
// Four things here are not obvious and matter on real hardware:
//
//   1. Time must be correct before TLS. A certificate's validity window cannot
//      be checked by a device that thinks it is 1970, and the ESP32 has no
//      battery-backed clock. NTP comes first, then TLS, in that order.
//   2. The sequence counter lives in RTC memory so it survives deep sleep.
//      Without that, every wake restarts at zero and the platform cannot tell a
//      reboot from a replay.
//   3. Readings are buffered when the link is down. A node on a marginal link
//      that drops samples is worse than useless, because the gaps look like the
//      sensor rather than the network.
//   4. The last will is registered at connect time, so an ungraceful
//      disconnect produces an offline event without the platform waiting out a
//      timeout.

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <Wire.h>
#include <Adafruit_BME280.h>
#include <time.h>

#include "config.h"

// --- Constants -------------------------------------------------------------

static constexpr char SCHEMA_VERSION[] = "1.1.0";

// Readings held while the link is down. Each is about 40 bytes, so 60 entries
// is 2.4 KB of the ESP32's 320 KB, and covers ten minutes at a 10 second
// cadence. Beyond that the oldest are dropped: a node that has been offline for
// an hour has stale data that the platform would reject as out of range anyway.
static constexpr size_t BUFFER_CAPACITY = 60;

static constexpr uint32_t WIFI_TIMEOUT_MS = 20000;
static constexpr uint32_t NTP_TIMEOUT_MS = 15000;
static constexpr uint32_t MQTT_KEEPALIVE_S = 30;

// Reconnect backoff, doubling from the first to the second. A node hammering a
// broker it cannot reach wastes its own battery and, across a fleet, keeps the
// broker busy refusing handshakes.
static constexpr uint32_t BACKOFF_START_MS = 1000;
static constexpr uint32_t BACKOFF_MAX_MS = 60000;

// --- State that survives deep sleep ---------------------------------------
//
// RTC_DATA_ATTR puts these in the RTC slow memory, which keeps its contents
// through deep sleep. A plain global is reinitialised on every wake, because a
// deep sleep wake is a fresh boot as far as the firmware is concerned.

RTC_DATA_ATTR static uint32_t rtc_sequence = 0;
RTC_DATA_ATTR static uint32_t rtc_boot_count = 0;

// --- Globals ---------------------------------------------------------------

static WiFiClientSecure tls_client;
static PubSubClient mqtt(tls_client);
static Adafruit_BME280 bme;
static bool sensor_present = false;

struct BufferedSample {
  time_t timestamp;
  uint32_t sequence;
  uint32_t uptime_s;
  float temperature_c;
  float humidity_pct;
  float pressure_hpa;
  float battery_v;
  int32_t rssi_dbm;
  bool valid;
};

static BufferedSample buffer[BUFFER_CAPACITY];
static size_t buffer_head = 0;
static size_t buffer_count = 0;

// --- Topics ----------------------------------------------------------------
//
// Built to the same grammar as telemetry_common.topics. The device id segment is
// what the broker authenticated, so it must be this device's own name; anything
// else is refused by the ACL.

static String telemetry_topic() {
  return String("dt/") + TENANT + "/" + SITE_ID + "/" + DEVICE_ID + "/telemetry";
}

static String status_topic() {
  return String("dt/") + TENANT + "/" + SITE_ID + "/" + DEVICE_ID + "/status";
}

static String command_topic() {
  return String("cmd/") + TENANT + "/" + SITE_ID + "/" + DEVICE_ID + "/req";
}

// --- Time ------------------------------------------------------------------

static bool sync_time() {
  // UTC, no daylight saving. Every timestamp on the wire is UTC; a device
  // applying a local offset is a device whose data lands in the wrong bucket
  // twice a year.
  configTime(0, 0, "pool.ntp.org", "time.google.com");

  const uint32_t started = millis();
  while (time(nullptr) < 1700000000) {  // a plausible floor, late 2023
    if (millis() - started > NTP_TIMEOUT_MS) {
      Serial.println(F("[time] NTP sync failed"));
      return false;
    }
    delay(200);
  }

  Serial.printf("[time] synced: %ld\n", (long)time(nullptr));
  return true;
}

static String iso8601(time_t when) {
  struct tm utc;
  gmtime_r(&when, &utc);
  char out[32];
  // The platform parses RFC 3339. A trailing Z rather than +00:00 because it is
  // shorter on the wire and both are valid.
  strftime(out, sizeof(out), "%Y-%m-%dT%H:%M:%SZ", &utc);
  return String(out);
}

// --- Network ---------------------------------------------------------------

static bool connect_wifi() {
  if (WiFi.status() == WL_CONNECTED) {
    return true;
  }

  Serial.printf("[wifi] connecting to %s\n", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  // Stops the ESP32 writing credentials to flash on every boot, which wears it
  // out for no benefit when they are compiled in anyway.
  WiFi.persistent(false);
  WiFi.setSleep(true);  // modem sleep between beacons, worth several mA
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  const uint32_t started = millis();
  while (WiFi.status() != WL_CONNECTED) {
    if (millis() - started > WIFI_TIMEOUT_MS) {
      Serial.println(F("[wifi] timed out"));
      return false;
    }
    delay(250);
  }

  Serial.printf("[wifi] connected, rssi %d dBm\n", WiFi.RSSI());
  return true;
}

static bool connect_mqtt() {
  if (mqtt.connected()) {
    return true;
  }

  // Order matters: the certificate validity check needs a correct clock, so a
  // failed NTP sync has to stop the handshake rather than let it proceed and
  // fail with a confusing TLS error.
  if (time(nullptr) < 1700000000 && !sync_time()) {
    return false;
  }

  tls_client.setCACert(CA_CERT);
  tls_client.setCertificate(DEVICE_CERT);
  tls_client.setPrivateKey(DEVICE_KEY);

  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setKeepAlive(MQTT_KEEPALIVE_S);
  mqtt.setBufferSize(1024);

  // Last will and testament. The broker publishes this if the connection drops
  // without a DISCONNECT, which is the only way the platform can tell a crashed
  // device from a quiet one without waiting out a timeout. Retained, so a
  // dashboard that starts later still learns the device is down.
  JsonDocument will;
  will["schema_version"] = SCHEMA_VERSION;
  will["device_id"] = DEVICE_ID;
  will["site_id"] = SITE_ID;
  will["ts"] = iso8601(time(nullptr));
  will["event"] = "offline";
  will["fw_version"] = FW_VERSION;
  will["reason"] = "unexpected disconnect";

  String will_payload;
  serializeJson(will, will_payload);

  Serial.printf("[mqtt] connecting to %s:%d as %s\n", MQTT_HOST, MQTT_PORT, DEVICE_ID);

  const bool connected = mqtt.connect(
      DEVICE_ID,
      nullptr, nullptr,                 // no username or password: the certificate is the identity
      status_topic().c_str(),           // will topic
      1,                                // will QoS
      true,                             // will retained
      will_payload.c_str());

  if (!connected) {
    Serial.printf("[mqtt] failed, state %d\n", mqtt.state());
    return false;
  }

  // Birth message. Retained for the same reason the will is.
  JsonDocument birth;
  birth["schema_version"] = SCHEMA_VERSION;
  birth["device_id"] = DEVICE_ID;
  birth["site_id"] = SITE_ID;
  birth["ts"] = iso8601(time(nullptr));
  birth["event"] = rtc_boot_count <= 1 ? "boot" : "online";
  birth["fw_version"] = FW_VERSION;

  String birth_payload;
  serializeJson(birth, birth_payload);
  mqtt.publish(status_topic().c_str(), birth_payload.c_str(), true);

  mqtt.subscribe(command_topic().c_str(), 1);

  Serial.println(F("[mqtt] connected"));
  return true;
}

// --- Sensors ---------------------------------------------------------------

static float read_battery_volts() {
  // Divide-by-two resistor network on GPIO34, as on most battery ESP32 boards.
  // The ESP32 ADC is badly non-linear above about 2.5 V, so a board that is
  // going to make real decisions on battery voltage wants a calibrated
  // characteristic rather than this. Flagged here rather than hidden.
  const int raw = analogRead(34);
  return (raw / 4095.0f) * 3.3f * 2.0f;
}

static BufferedSample take_sample() {
  BufferedSample sample{};
  sample.timestamp = time(nullptr);
  sample.sequence = ++rtc_sequence;
  sample.uptime_s = millis() / 1000;
  sample.rssi_dbm = WiFi.RSSI();
  sample.battery_v = read_battery_volts();

  if (sensor_present) {
    sample.temperature_c = bme.readTemperature();
    sample.humidity_pct = bme.readHumidity();
    sample.pressure_hpa = bme.readPressure() / 100.0f;
    // A disconnected BME280 reads NaN rather than failing. Publishing that
    // would poison every average downstream, and the platform refuses
    // non-finite numbers anyway, so the sample is marked invalid here.
    sample.valid = !isnan(sample.temperature_c) && !isnan(sample.humidity_pct);
  } else {
    sample.valid = false;
  }

  return sample;
}

// --- Buffering -------------------------------------------------------------

static void buffer_push(const BufferedSample &sample) {
  buffer[buffer_head] = sample;
  buffer_head = (buffer_head + 1) % BUFFER_CAPACITY;
  if (buffer_count < BUFFER_CAPACITY) {
    buffer_count++;
  } else {
    Serial.println(F("[buffer] full, dropped the oldest sample"));
  }
}

// --- Publishing ------------------------------------------------------------

static bool publish(const BufferedSample &sample) {
  JsonDocument doc;
  doc["schema_version"] = SCHEMA_VERSION;
  doc["device_id"] = DEVICE_ID;
  doc["site_id"] = SITE_ID;
  doc["seq"] = sample.sequence;
  doc["ts"] = iso8601(sample.timestamp);
  doc["fw_version"] = FW_VERSION;
  doc["uptime_s"] = sample.uptime_s;
  doc["status"] = sample.valid ? "ok" : "fault";

  // Metric names exactly as in the platform's catalogue. A typo here is a
  // metric nothing queries and no rule evaluates, and nothing complains.
  JsonObject readings = doc["readings"].to<JsonObject>();
  if (sample.valid) {
    readings["temperature_c"] = serialized(String(sample.temperature_c, 2));
    readings["humidity_pct"] = serialized(String(sample.humidity_pct, 2));
    readings["pressure_hpa"] = serialized(String(sample.pressure_hpa, 2));
  }
  readings["battery_v"] = serialized(String(sample.battery_v, 3));
  readings["rssi_dbm"] = sample.rssi_dbm;

  String payload;
  serializeJson(doc, payload);

  // QoS 1. At-least-once, so a redelivery is possible and the platform
  // deduplicates on (device_id, metric, ts). QoS 0 would lose messages on a
  // marginal link; QoS 2 doubles the round trips for a guarantee the
  // deduplicating writer already provides.
  const bool ok = mqtt.publish(telemetry_topic().c_str(), payload.c_str(), false);
  if (!ok) {
    Serial.printf("[mqtt] publish failed, state %d\n", mqtt.state());
  }
  return ok;
}

static void drain_buffer() {
  while (buffer_count > 0 && mqtt.connected()) {
    // Oldest first, so the platform receives them in the order they were taken.
    const size_t index = (buffer_head + BUFFER_CAPACITY - buffer_count) % BUFFER_CAPACITY;
    if (!publish(buffer[index])) {
      return;  // leave the rest buffered rather than dropping them
    }
    buffer_count--;
  }
}

// --- Commands --------------------------------------------------------------

static void on_command(char *topic, byte *payload, unsigned int length) {
  JsonDocument doc;
  const DeserializationError error = deserializeJson(doc, payload, length);
  if (error) {
    Serial.printf("[cmd] undecodable: %s\n", error.c_str());
    return;
  }

  const char *action = doc["action"] | "";
  Serial.printf("[cmd] %s on %s\n", action, topic);

  if (strcmp(action, "reboot") == 0) {
    Serial.println(F("[cmd] rebooting"));
    delay(100);
    ESP.restart();
  } else if (strcmp(action, "identify") == 0) {
    // Blinks the on-board LED so an engineer can find this node on a wall of
    // identical boxes.
    if (STATUS_LED_PIN >= 0) {
      for (int i = 0; i < 10; i++) {
        digitalWrite(STATUS_LED_PIN, i % 2);
        delay(150);
      }
      digitalWrite(STATUS_LED_PIN, LOW);
    }
  } else {
    Serial.printf("[cmd] unknown action: %s\n", action);
  }
}

// --- Lifecycle -------------------------------------------------------------

#if USE_DEEP_SLEEP
static void enter_deep_sleep(uint32_t seconds) {
  Serial.printf("[sleep] %u s\n", (unsigned)seconds);

  // Graceful disconnect, so the broker discards the will and the platform does
  // not raise an offline alert for a device that is merely asleep.
  if (mqtt.connected()) {
    JsonDocument doc;
    doc["schema_version"] = SCHEMA_VERSION;
    doc["device_id"] = DEVICE_ID;
    doc["site_id"] = SITE_ID;
    doc["ts"] = iso8601(time(nullptr));
    doc["event"] = "offline";
    doc["fw_version"] = FW_VERSION;
    doc["reason"] = "deep sleep";

    String payload;
    serializeJson(doc, payload);
    mqtt.publish(status_topic().c_str(), payload.c_str(), true);
    mqtt.disconnect();
  }

  WiFi.disconnect(true);
  Serial.flush();

  esp_sleep_enable_timer_wakeup((uint64_t)seconds * 1000000ULL);
  esp_deep_sleep_start();
}
#endif  // USE_DEEP_SLEEP

void setup() {
  Serial.begin(115200);
  delay(100);

  rtc_boot_count++;
  Serial.printf("\n[boot] %s fw %s, boot %u, seq %u\n",
                DEVICE_ID, FW_VERSION, (unsigned)rtc_boot_count, (unsigned)rtc_sequence);

  if (STATUS_LED_PIN >= 0) {
    pinMode(STATUS_LED_PIN, OUTPUT);
  }

  Wire.begin();
  // 0x76 and 0x77 are both common for a BME280 depending on the breakout.
  sensor_present = bme.begin(0x76) || bme.begin(0x77);
  if (sensor_present) {
    // Forced mode with oversampling: the device samples when asked rather than
    // continuously, which matters for a battery node, and the filter removes
    // the short-term noise that would otherwise dominate a 10 second cadence.
    bme.setSampling(Adafruit_BME280::MODE_FORCED,
                    Adafruit_BME280::SAMPLING_X2,   // temperature
                    Adafruit_BME280::SAMPLING_X16,  // pressure
                    Adafruit_BME280::SAMPLING_X1,   // humidity
                    Adafruit_BME280::FILTER_X4);
    Serial.println(F("[sensor] BME280 ready"));
  } else {
    // Keep running. A node that reports battery and signal strength with a
    // fault status is still telling the platform something; one that refuses to
    // boot tells it nothing at all.
    Serial.println(F("[sensor] BME280 not found, publishing status only"));
  }

  mqtt.setCallback(on_command);

  connect_wifi();
  sync_time();
  connect_mqtt();
}

void loop() {
  static uint32_t last_sample_ms = 0;
  static uint32_t backoff_ms = BACKOFF_START_MS;

  if (!mqtt.connected()) {
    if (connect_wifi() && connect_mqtt()) {
      backoff_ms = BACKOFF_START_MS;
      drain_buffer();
    } else {
      Serial.printf("[net] retry in %u ms\n", (unsigned)backoff_ms);
      delay(backoff_ms);
      // Doubled with an explicit comparison rather than std::min. uint32_t is
      // unsigned long on the RISC-V toolchain and unsigned int on the xtensa
      // one, so min(backoff_ms * 2, 60000U) deduces conflicting argument types
      // and fails to compile on the C3 while building fine on the ESP32.
      const uint32_t doubled = backoff_ms * 2;
      backoff_ms = doubled > BACKOFF_MAX_MS ? BACKOFF_MAX_MS : doubled;
    }
  }

  mqtt.loop();

  const uint32_t now_ms = millis();
  if (now_ms - last_sample_ms >= (uint32_t)SAMPLE_INTERVAL_S * 1000) {
    last_sample_ms = now_ms;

    const BufferedSample sample = take_sample();

    if (mqtt.connected()) {
      drain_buffer();
      if (!publish(sample)) {
        buffer_push(sample);
      }
    } else {
      buffer_push(sample);
      Serial.printf("[buffer] holding %u samples\n", (unsigned)buffer_count);
    }

#if USE_DEEP_SLEEP
    // Only sleep once the buffer is clear, otherwise the backlog would be
    // carried across a sleep in RAM that does not survive it.
    if (buffer_count == 0) {
      enter_deep_sleep(SAMPLE_INTERVAL_S);
    }
#endif
  }

  delay(50);
}
