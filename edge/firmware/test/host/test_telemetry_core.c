// Host tests for the hardware-independent half of the firmware.
//
//   make -C edge/firmware/test/host
//
// Compiled with the host compiler, with AddressSanitizer and UBSan, from the
// same sources the firmware links. No framework: a failed CHECK prints where
// and the run exits non-zero, which is all CI needs.

#include <math.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "telemetry_core.h"

static int failures = 0;
static int checks = 0;

#define CHECK(cond)                                                       \
    do {                                                                  \
        checks++;                                                         \
        if (!(cond)) {                                                    \
            failures++;                                                   \
            fprintf(stderr, "%s:%d: CHECK failed: %s\n", __FILE__, __LINE__, #cond); \
        }                                                                 \
    } while (0)

#define CHECK_STR(actual, expected)                                       \
    do {                                                                  \
        checks++;                                                         \
        if (strcmp((actual), (expected)) != 0) {                          \
            failures++;                                                   \
            fprintf(stderr, "%s:%d: expected\n  %s\ngot\n  %s\n", __FILE__, __LINE__, (expected), (actual)); \
        }                                                                 \
    } while (0)

static const tc_identity_t IDENT = {"acme", "site-q", "esp32-qemu-0001"};

static void test_segments(void)
{
    CHECK(tc_segment_valid("esp32-000001"));
    CHECK(tc_segment_valid("abc"));
    CHECK(tc_segment_valid("0ab"));
    CHECK(!tc_segment_valid("ab"));
    CHECK(!tc_segment_valid("-abc"));
    CHECK(!tc_segment_valid("Abc"));
    CHECK(!tc_segment_valid("a_bc"));
    CHECK(!tc_segment_valid("a/bc"));
    CHECK(!tc_segment_valid("a+bc"));
    CHECK(!tc_segment_valid(""));
    CHECK(!tc_segment_valid(NULL));

    char long_id[70];
    memset(long_id, 'a', 64);
    long_id[64] = '\0';
    CHECK(tc_segment_valid(long_id));
    long_id[64] = 'a';
    long_id[65] = '\0';
    CHECK(!tc_segment_valid(long_id));

    CHECK(tc_identity_valid(&IDENT));
    tc_identity_t bad = IDENT;
    strcpy(bad.site_id, "Site A");
    CHECK(!tc_identity_valid(&bad));
}

static void test_topics(void)
{
    char out[128];
    CHECK(tc_topic_telemetry(&IDENT, out, sizeof out) > 0);
    CHECK_STR(out, "dt/acme/site-q/esp32-qemu-0001/telemetry");
    CHECK(tc_topic_status(&IDENT, out, sizeof out) > 0);
    CHECK_STR(out, "dt/acme/site-q/esp32-qemu-0001/status");
    CHECK(tc_topic_command(&IDENT, out, sizeof out) > 0);
    CHECK_STR(out, "cmd/acme/site-q/esp32-qemu-0001/req");

    // Truncation is a failure, never a shorter topic.
    char tiny[16];
    CHECK(tc_topic_telemetry(&IDENT, tiny, sizeof tiny) == -1);
    CHECK_STR(tiny, "");
}

static void test_time(void)
{
    char out[32];
    CHECK(tc_format_iso8601((time_t)1700000000, out, sizeof out) == 20);
    CHECK_STR(out, "2023-11-14T22:13:20Z");
    CHECK(!tc_clock_plausible((time_t)0));
    CHECK(!tc_clock_plausible((time_t)86400));
    CHECK(tc_clock_plausible((time_t)1700000000));
}

static tc_sample_t sample_at(uint32_t seq)
{
    tc_sample_t s;
    memset(&s, 0, sizeof s);
    s.timestamp = (time_t)1700000000 + seq;
    s.seq = seq;
    s.uptime_s = 30 + seq;
    s.temperature_c = 21.5f;
    s.humidity_pct = 44.25f;
    s.pressure_hpa = 1013.5f;
    s.battery_v = 3.875f;
    s.rssi_dbm = -61;
    s.has_environment = true;
    s.has_battery = true;
    s.has_rssi = true;
    return s;
}

static void test_telemetry(void)
{
    char out[512];
    tc_sample_t s = sample_at(7);
    CHECK(tc_format_telemetry(&IDENT, "2.0.0", &s, out, sizeof out) > 0);
    CHECK_STR(out,
              "{\"schema_version\":\"1.1.0\",\"device_id\":\"esp32-qemu-0001\",\"site_id\":\"site-q\","
              "\"seq\":7,\"ts\":\"2023-11-14T22:13:27Z\",\"fw_version\":\"2.0.0\",\"uptime_s\":37,"
              "\"status\":\"ok\",\"readings\":{\"temperature_c\":21.50,\"humidity_pct\":44.25,"
              "\"pressure_hpa\":1013.50,\"battery_v\":3.875,\"rssi_dbm\":-61}}");

    // The emulated build has no Wi-Fi and no ADC: environment only.
    s.has_battery = false;
    s.has_rssi = false;
    CHECK(tc_format_telemetry(&IDENT, "2.0.0", &s, out, sizeof out) > 0);
    CHECK(strstr(out, "\"readings\":{\"temperature_c\":21.50,\"humidity_pct\":44.25,\"pressure_hpa\":1013.50}}") != NULL);

    // No sensor: a fault status with what the device does still know.
    s = sample_at(8);
    s.has_environment = false;
    CHECK(tc_format_telemetry(&IDENT, "2.0.0", &s, out, sizeof out) > 0);
    CHECK(strstr(out, "\"status\":\"fault\"") != NULL);
    CHECK(strstr(out, "temperature_c") == NULL);
    CHECK(strstr(out, "\"battery_v\":3.875") != NULL);

    // A NaN from a disconnected sensor is never published.
    s = sample_at(9);
    s.temperature_c = NAN;
    CHECK(tc_format_telemetry(&IDENT, "2.0.0", &s, out, sizeof out) > 0);
    CHECK(strstr(out, "\"status\":\"fault\"") != NULL);
    CHECK(strstr(out, "nan") == NULL);
    CHECK(strstr(out, "temperature_c") == NULL);

    // Nothing to say at all: the platform requires at least one reading.
    s.has_battery = false;
    s.has_rssi = false;
    CHECK(tc_format_telemetry(&IDENT, "2.0.0", &s, out, sizeof out) == -1);

    // Too small a buffer fails rather than truncating the JSON.
    s = sample_at(10);
    char small[100];
    CHECK(tc_format_telemetry(&IDENT, "2.0.0", &s, small, sizeof small) == -1);
}

static void test_status(void)
{
    char out[1024];
    CHECK(tc_format_status(&IDENT, "2.0.0", (time_t)1700000000, "boot", NULL, out, sizeof out) > 0);
    CHECK_STR(out,
              "{\"schema_version\":\"1.1.0\",\"device_id\":\"esp32-qemu-0001\",\"site_id\":\"site-q\","
              "\"ts\":\"2023-11-14T22:13:20Z\",\"event\":\"boot\",\"fw_version\":\"2.0.0\"}");

    CHECK(tc_format_status(&IDENT, "2.0.0", (time_t)1700000000, "offline", "say \"hi\"\n\\", out,
                           sizeof out) > 0);
    CHECK(strstr(out, "\"reason\":\"say \\\"hi\\\"\\u000a\\\\\"}") != NULL);

    // The platform caps reason at 256 characters.
    char longer[400];
    memset(longer, 'r', sizeof longer - 1);
    longer[sizeof longer - 1] = '\0';
    CHECK(tc_format_status(&IDENT, "2.0.0", (time_t)1700000000, "boot", longer, out, sizeof out) > 0);
    const char *reason = strstr(out, "\"reason\":\"");
    CHECK(reason != NULL);
    if (reason != NULL) {
        reason += strlen("\"reason\":\"");
        CHECK(strspn(reason, "r") == 256);
    }
}

static void test_buffer(void)
{
    tc_sample_t storage[3];
    tc_buffer_t b;
    tc_buffer_init(&b, storage, 3);
    CHECK(tc_buffer_oldest(&b) == NULL);

    for (uint32_t i = 1; i <= 3; i++) {
        const tc_sample_t s = sample_at(i);
        CHECK(tc_buffer_push(&b, &s));
    }
    CHECK(tc_buffer_count(&b) == 3);
    CHECK(tc_buffer_oldest(&b)->seq == 1);

    // Full: the fourth overwrites the oldest and says so.
    const tc_sample_t s4 = sample_at(4);
    CHECK(!tc_buffer_push(&b, &s4));
    CHECK(b.dropped == 1);
    CHECK(tc_buffer_count(&b) == 3);

    // Drained oldest first.
    uint32_t expected = 2;
    while (tc_buffer_count(&b) > 0) {
        CHECK(tc_buffer_oldest(&b)->seq == expected);
        tc_buffer_pop(&b);
        expected++;
    }
    CHECK(expected == 5);
    tc_buffer_pop(&b);  // popping empty is harmless
    CHECK(tc_buffer_count(&b) == 0);
}

static void test_backoff(void)
{
    tc_backoff_t b;
    tc_backoff_init(&b, 1000, 60000);
    const uint32_t expected[] = {1000, 2000, 4000, 8000, 16000, 32000, 60000, 60000};
    for (size_t i = 0; i < sizeof expected / sizeof expected[0]; i++) {
        CHECK(tc_backoff_next(&b) == expected[i]);
    }
    tc_backoff_reset(&b);
    CHECK(tc_backoff_next(&b) == 1000);

    // No overflow near the top of the range.
    tc_backoff_init(&b, 3000000000u, 4000000000u);
    CHECK(tc_backoff_next(&b) == 3000000000u);
    CHECK(tc_backoff_next(&b) == 4000000000u);
}

static tc_command_t parse(const char *s)
{
    return tc_parse_command(s, strlen(s));
}

static void test_commands(void)
{
    CHECK(parse("{\"action\":\"reboot\"}") == TC_CMD_REBOOT);
    CHECK(parse(" { \"action\" : \"identify\" } ") == TC_CMD_IDENTIFY);
    CHECK(parse("{\"id\":\"c-1\",\"args\":{\"a\":[1,{\"b\":\"}\"}]},\"action\":\"identify\"}") == TC_CMD_IDENTIFY);
    CHECK(parse("{\"ttl_s\":30,\"action\":\"reboot\"}") == TC_CMD_REBOOT);
    CHECK(parse("{\"action\":\"selfdestruct\"}") == TC_CMD_UNKNOWN);
    CHECK(parse("{\"action\":\"reboots\"}") == TC_CMD_UNKNOWN);
    CHECK(parse("{\"action\":1}") == TC_CMD_INVALID);
    CHECK(parse("{\"other\":\"reboot\"}") == TC_CMD_INVALID);
    CHECK(parse("{}") == TC_CMD_INVALID);
    CHECK(parse("[\"reboot\"]") == TC_CMD_INVALID);
    CHECK(parse("{\"action\":\"reboot") == TC_CMD_INVALID);
    CHECK(parse("") == TC_CMD_INVALID);
    CHECK(tc_parse_command(NULL, 0) == TC_CMD_INVALID);
    // Length is respected: the payload is not NUL terminated off the wire.
    const char *cut = "{\"action\":\"reboot\"}garbage";
    CHECK(tc_parse_command(cut, 19) == TC_CMD_REBOOT);
    CHECK(tc_parse_command(cut, 12) == TC_CMD_INVALID);
}

// --- BME280 ----------------------------------------------------------------

// The datasheet's floating point compensation (BME280 datasheet section 8.1),
// an independent formulation of the same model, used to check the integer one.
static double float_temperature(const tc_bme280_calib_t *c, int32_t adc_t, double *t_fine)
{
    const double var1 = ((double)adc_t / 16384.0 - (double)c->dig_T1 / 1024.0) * (double)c->dig_T2;
    const double d = (double)adc_t / 131072.0 - (double)c->dig_T1 / 8192.0;
    const double var2 = d * d * (double)c->dig_T3;
    *t_fine = var1 + var2;
    return (var1 + var2) / 5120.0;
}

static double float_pressure(const tc_bme280_calib_t *c, int32_t adc_p, double t_fine)
{
    double var1 = t_fine / 2.0 - 64000.0;
    double var2 = var1 * var1 * (double)c->dig_P6 / 32768.0;
    var2 = var2 + var1 * (double)c->dig_P5 * 2.0;
    var2 = var2 / 4.0 + (double)c->dig_P4 * 65536.0;
    var1 = ((double)c->dig_P3 * var1 * var1 / 524288.0 + (double)c->dig_P2 * var1) / 524288.0;
    var1 = (1.0 + var1 / 32768.0) * (double)c->dig_P1;
    if (var1 == 0.0) {
        return 0.0;
    }
    double p = 1048576.0 - (double)adc_p;
    p = (p - var2 / 4096.0) * 6250.0 / var1;
    var1 = (double)c->dig_P9 * p * p / 2147483648.0;
    var2 = p * (double)c->dig_P8 / 32768.0;
    return p + (var1 + var2 + (double)c->dig_P7) / 16.0;
}

static double float_humidity(const tc_bme280_calib_t *c, int32_t adc_h, double t_fine)
{
    double h = t_fine - 76800.0;
    h = ((double)adc_h - ((double)c->dig_H4 * 64.0 + (double)c->dig_H5 / 16384.0 * h)) *
        ((double)c->dig_H2 / 65536.0 *
         (1.0 + (double)c->dig_H6 / 67108864.0 * h * (1.0 + (double)c->dig_H3 / 67108864.0 * h)));
    h = h * (1.0 - (double)c->dig_H1 * h / 524288.0);
    if (h > 100.0) {
        h = 100.0;
    } else if (h < 0.0) {
        h = 0.0;
    }
    return h;
}

static void put_u16(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)(v & 0xFF);
    p[1] = (uint8_t)(v >> 8);
}

static void test_bme280_calibration_layout(void)
{
    uint8_t block1[26] = {0};
    uint8_t block2[7] = {0};
    put_u16(&block1[0], 27504);
    put_u16(&block1[2], (uint16_t)26435);
    put_u16(&block1[4], (uint16_t)(int16_t)-1000);
    put_u16(&block1[6], 36477);
    put_u16(&block1[8], (uint16_t)(int16_t)-10685);
    put_u16(&block1[22], 6000);
    block1[25] = 75;

    // H2 = 362, H3 = 0, H4 = 313 (0x139), H5 = -50 (0xFCE), H6 = -3.
    put_u16(&block2[0], 362);
    block2[2] = 0;
    block2[3] = 0x13;                // H4 bits 11:4
    block2[4] = (uint8_t)(0xE0 | 0x9);  // H5 bits 3:0 in the high nibble, H4 bits 3:0 low
    block2[5] = 0xFC;                // H5 bits 11:4
    block2[6] = (uint8_t)(int8_t)-3;

    tc_bme280_calib_t c;
    tc_bme280_parse_calib(block1, block2, &c);
    CHECK(c.dig_T1 == 27504);
    CHECK(c.dig_T2 == 26435);
    CHECK(c.dig_T3 == -1000);
    CHECK(c.dig_P1 == 36477);
    CHECK(c.dig_P2 == -10685);
    CHECK(c.dig_P9 == 6000);
    CHECK(c.dig_H1 == 75);
    CHECK(c.dig_H2 == 362);
    CHECK(c.dig_H3 == 0);
    CHECK(c.dig_H4 == 313);
    CHECK(c.dig_H5 == -50);
    CHECK(c.dig_H6 == -3);

    const uint8_t raw[8] = {0x65, 0x5A, 0xC0, 0x7E, 0xED, 0x00, 0x6E, 0x8F};
    int32_t p, t, h;
    tc_bme280_parse_raw(raw, &p, &t, &h);
    CHECK(p == 415148);
    CHECK(t == 519888);
    CHECK(h == 0x6E8F);
}

// Coefficients from the worked example in the Bosch BMP280 datasheet (section
// 3.12), whose temperature and pressure compensation is identical to the
// BME280's. The humidity coefficients are not from a datasheet: they are only
// a plausible set to drive the cross-check between the two formulations.
static const tc_bme280_calib_t EXAMPLE = {
    .dig_T1 = 27504, .dig_T2 = 26435, .dig_T3 = -1000,
    .dig_P1 = 36477, .dig_P2 = -10685, .dig_P3 = 3024, .dig_P4 = 2855, .dig_P5 = 140,
    .dig_P6 = -7, .dig_P7 = 15500, .dig_P8 = -14600, .dig_P9 = 6000,
    .dig_H1 = 75, .dig_H2 = 362, .dig_H3 = 0, .dig_H4 = 313, .dig_H5 = 50, .dig_H6 = 30,
};

static void test_bme280_worked_example(void)
{
    int32_t t_fine;
    const int32_t t = tc_bme280_temperature(&EXAMPLE, 519888, &t_fine);
    CHECK(t == 2508);  // 25.08 degC
    const uint32_t p = tc_bme280_pressure(&EXAMPLE, 415148, t_fine);
    CHECK(p / 256 == 100653);  // 100653 Pa
}

static void test_bme280_integer_matches_float(void)
{
    double worst_t = 0.0;
    double worst_p = 0.0;
    double worst_h = 0.0;
    for (int32_t adc_t = 480000; adc_t <= 560000; adc_t += 4000) {
        int32_t t_fine;
        const double t_int = tc_bme280_temperature(&EXAMPLE, adc_t, &t_fine) / 100.0;
        double t_fine_f;
        const double t_flt = float_temperature(&EXAMPLE, adc_t, &t_fine_f);
        worst_t = fmax(worst_t, fabs(t_int - t_flt));

        for (int32_t adc_p = 300000; adc_p <= 500000; adc_p += 10000) {
            const double p_int = tc_bme280_pressure(&EXAMPLE, adc_p, t_fine) / 256.0;
            const double p_flt = float_pressure(&EXAMPLE, adc_p, (double)t_fine);
            worst_p = fmax(worst_p, fabs(p_int - p_flt));
        }
        for (int32_t adc_h = 20000; adc_h <= 40000; adc_h += 1000) {
            const double h_int = tc_bme280_humidity(&EXAMPLE, adc_h, t_fine) / 1024.0;
            const double h_flt = float_humidity(&EXAMPLE, adc_h, (double)t_fine);
            worst_h = fmax(worst_h, fabs(h_int - h_flt));
        }
    }
    printf("  bme280 integer vs float, worst difference: T %.4f degC, P %.3f Pa, H %.4f %%RH\n", worst_t,
           worst_p, worst_h);
    CHECK(worst_t <= 0.011);
    CHECK(worst_p <= 1.0);
    CHECK(worst_h <= 0.05);
}

static void test_simulated_environment(void)
{
    // Inside the platform's plausible ranges at every second of a day, so the
    // emulated device can never be dead-lettered as implausible.
    bool in_range = true;
    for (uint32_t s = 0; s < 86400; s++) {
        float t, h, p;
        tc_simulated_environment(s, &t, &h, &p);
        in_range = in_range && t > -40.0f && t < 125.0f && h >= 0.0f && h <= 100.0f && p >= 300.0f &&
                   p <= 1100.0f;
    }
    CHECK(in_range);
}

int main(void)
{
    test_segments();
    test_topics();
    test_time();
    test_telemetry();
    test_status();
    test_buffer();
    test_backoff();
    test_commands();
    test_bme280_calibration_layout();
    test_bme280_worked_example();
    test_bme280_integer_matches_float();
    test_simulated_environment();

    if (failures != 0) {
        fprintf(stderr, "%d of %d checks failed\n", failures, checks);
        return 1;
    }
    printf("all %d checks passed\n", checks);
    return 0;
}
