// The parts of the firmware that do not touch hardware.
//
// Topic grammar, the JSON the platform accepts, the offline buffer, reconnect
// backoff, command parsing and the BME280 compensation arithmetic. None of it
// includes an ESP-IDF header, so the same sources compile into the firmware and
// into the host test binary under test/host, which runs in CI on every push.
//
// Everything here must agree with services/common/telemetry_common: the topic
// grammar in topics.py and the wire models in models.py. The emulated device
// test in tests/integration proves the agreement end to end, because a message
// that disagrees is dead-lettered by ingest and the test asserts there are none.

#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <time.h>

#ifdef __cplusplus
extern "C" {
#endif

// Matches CURRENT_SCHEMA_VERSION in telemetry_common/versioning.py.
#define TC_SCHEMA_VERSION "1.1.0"

// A topic segment is at most 64 characters; one more for the terminator.
#define TC_SEGMENT_MAX 65

// --- Identity and topics ---------------------------------------------------

typedef struct {
    char tenant[TC_SEGMENT_MAX];
    char site_id[TC_SEGMENT_MAX];
    char device_id[TC_SEGMENT_MAX];
} tc_identity_t;

// True when the segment matches ^[a-z0-9][a-z0-9-]{2,63}$, the grammar every
// tenant, site and device segment must follow. Checked once at boot on the
// provisioned identity, so nothing later has to escape these strings.
bool tc_segment_valid(const char *segment);

// True when all three segments of the identity are valid.
bool tc_identity_valid(const tc_identity_t *id);

// Each returns the number of characters written, or -1 if the buffer was too
// small. A truncated topic is never returned.
int tc_topic_telemetry(const tc_identity_t *id, char *out, size_t out_len);
int tc_topic_status(const tc_identity_t *id, char *out, size_t out_len);
int tc_topic_command(const tc_identity_t *id, char *out, size_t out_len);

// --- Time ------------------------------------------------------------------

// Earliest wall clock the firmware believes. Anything before it means SNTP has
// not run yet and a TLS handshake must not be attempted: certificate validity
// cannot be checked by a device that thinks it is 1970.
#define TC_PLAUSIBLE_EPOCH ((time_t)1700000000)

bool tc_clock_plausible(time_t now);

// RFC 3339 in UTC with a trailing Z. Returns characters written or -1.
int tc_format_iso8601(time_t when, char *out, size_t out_len);

// --- Messages --------------------------------------------------------------

typedef struct {
    time_t timestamp;
    uint32_t seq;
    uint32_t uptime_s;
    float temperature_c;
    float humidity_pct;
    float pressure_hpa;
    float battery_v;
    int32_t rssi_dbm;
    bool has_environment;  // temperature, humidity and pressure are valid
    bool has_battery;
    bool has_rssi;
} tc_sample_t;

// A telemetry message as TelemetryMessage in models.py expects it. status is
// "ok" when the environmental readings are present and "fault" otherwise.
// Returns characters written, or -1 if the buffer was too small or the sample
// carries no readings at all (the platform requires at least one).
int tc_format_telemetry(const tc_identity_t *id, const char *fw_version, const tc_sample_t *sample,
                        char *out, size_t out_len);

// A lifecycle event as StatusMessage in models.py expects it. reason may be
// NULL; when present it is JSON-escaped and truncated to 256 characters, the
// limit the platform enforces.
int tc_format_status(const tc_identity_t *id, const char *fw_version, time_t when, const char *event,
                     const char *reason, char *out, size_t out_len);

// --- Offline buffer --------------------------------------------------------
//
// A fixed ring of samples held while the link is down. When it is full the
// oldest sample is overwritten: data from an hour ago is worth less than data
// from now and the platform accepts a backlog up to seven days old anyway.

typedef struct {
    tc_sample_t *slots;
    size_t capacity;
    size_t head;   // next slot to write
    size_t count;  // samples held
    uint32_t dropped;
} tc_buffer_t;

void tc_buffer_init(tc_buffer_t *buffer, tc_sample_t *storage, size_t capacity);
// Returns false when the push overwrote the oldest sample.
bool tc_buffer_push(tc_buffer_t *buffer, const tc_sample_t *sample);
// The oldest held sample, or NULL when empty. Valid until the next push or pop.
const tc_sample_t *tc_buffer_oldest(const tc_buffer_t *buffer);
void tc_buffer_pop(tc_buffer_t *buffer);
size_t tc_buffer_count(const tc_buffer_t *buffer);

// --- Reconnect backoff -----------------------------------------------------

typedef struct {
    uint32_t start_ms;
    uint32_t max_ms;
    uint32_t current_ms;
} tc_backoff_t;

void tc_backoff_init(tc_backoff_t *backoff, uint32_t start_ms, uint32_t max_ms);
// The delay to wait now; the next call returns double, capped at max_ms.
uint32_t tc_backoff_next(tc_backoff_t *backoff);
void tc_backoff_reset(tc_backoff_t *backoff);

// --- Commands --------------------------------------------------------------

typedef enum {
    TC_CMD_INVALID = 0,  // not a JSON object with a string "action"
    TC_CMD_UNKNOWN,      // well formed but an action this firmware does not know
    TC_CMD_REBOOT,
    TC_CMD_IDENTIFY,
} tc_command_t;

// Reads the top-level "action" member of a command payload. Not a general JSON
// parser: it understands exactly the shape the command dispatcher sends.
tc_command_t tc_parse_command(const char *payload, size_t len);

// --- BME280 compensation ---------------------------------------------------
//
// The integer formulas from the Bosch BME280 datasheet, section 4.2.3, kept
// apart from the I2C driver so they can be checked on the host against the
// datasheet's floating point formulas and its worked example.

typedef struct {
    uint16_t dig_T1;
    int16_t dig_T2;
    int16_t dig_T3;
    uint16_t dig_P1;
    int16_t dig_P2;
    int16_t dig_P3;
    int16_t dig_P4;
    int16_t dig_P5;
    int16_t dig_P6;
    int16_t dig_P7;
    int16_t dig_P8;
    int16_t dig_P9;
    uint8_t dig_H1;
    int16_t dig_H2;
    uint8_t dig_H3;
    int16_t dig_H4;
    int16_t dig_H5;
    int8_t dig_H6;
} tc_bme280_calib_t;

// block1 is registers 0x88 to 0xA1 (26 bytes); block2 is 0xE1 to 0xE7 (7 bytes).
void tc_bme280_parse_calib(const uint8_t block1[26], const uint8_t block2[7], tc_bme280_calib_t *out);

// Burst read of 0xF7 to 0xFE into the three raw ADC values.
void tc_bme280_parse_raw(const uint8_t data[8], int32_t *adc_p, int32_t *adc_t, int32_t *adc_h);

// Temperature in 0.01 degC. Also returns t_fine, which the other two need.
int32_t tc_bme280_temperature(const tc_bme280_calib_t *cal, int32_t adc_t, int32_t *t_fine);
// Pressure in Pa as unsigned Q24.8, so divide by 256. 0 when the calibration
// would divide by zero.
uint32_t tc_bme280_pressure(const tc_bme280_calib_t *cal, int32_t adc_p, int32_t t_fine);
// Relative humidity in percent as unsigned Q22.10, so divide by 1024.
uint32_t tc_bme280_humidity(const tc_bme280_calib_t *cal, int32_t adc_h, int32_t t_fine);

// --- Simulated environment -------------------------------------------------
//
// Used only by the QEMU build, which has no I2C sensor to read. Smooth,
// deterministic and inside the platform's plausible ranges. Every value it
// produces is simulated and the device says so in its boot status message.
void tc_simulated_environment(uint32_t uptime_s, float *temperature_c, float *humidity_pct,
                              float *pressure_hpa);

#ifdef __cplusplus
}
#endif
