// Topics and wire messages. See telemetry_core.h.

#include <math.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#include "telemetry_core.h"

// The platform's limit on StatusMessage.reason.
#define REASON_MAX 256

bool tc_segment_valid(const char *segment)
{
    if (segment == NULL) {
        return false;
    }
    const size_t len = strlen(segment);
    if (len < 3 || len > 64) {
        return false;
    }
    for (size_t i = 0; i < len; i++) {
        const char c = segment[i];
        const bool lower = c >= 'a' && c <= 'z';
        const bool digit = c >= '0' && c <= '9';
        if (i == 0 && !(lower || digit)) {
            return false;
        }
        if (!(lower || digit || c == '-')) {
            return false;
        }
    }
    return true;
}

bool tc_identity_valid(const tc_identity_t *id)
{
    return id != NULL && tc_segment_valid(id->tenant) && tc_segment_valid(id->site_id) &&
           tc_segment_valid(id->device_id);
}

// snprintf that reports truncation as failure rather than as a shorter string.
static int bounded(char *out, size_t out_len, const char *fmt, ...)
{
    va_list args;
    va_start(args, fmt);
    const int written = vsnprintf(out, out_len, fmt, args);
    va_end(args);
    if (written < 0 || (size_t)written >= out_len) {
        if (out_len > 0) {
            out[0] = '\0';
        }
        return -1;
    }
    return written;
}

int tc_topic_telemetry(const tc_identity_t *id, char *out, size_t out_len)
{
    return bounded(out, out_len, "dt/%s/%s/%s/telemetry", id->tenant, id->site_id, id->device_id);
}

int tc_topic_status(const tc_identity_t *id, char *out, size_t out_len)
{
    return bounded(out, out_len, "dt/%s/%s/%s/status", id->tenant, id->site_id, id->device_id);
}

int tc_topic_command(const tc_identity_t *id, char *out, size_t out_len)
{
    return bounded(out, out_len, "cmd/%s/%s/%s/req", id->tenant, id->site_id, id->device_id);
}

bool tc_clock_plausible(time_t now)
{
    return now >= TC_PLAUSIBLE_EPOCH;
}

int tc_format_iso8601(time_t when, char *out, size_t out_len)
{
    struct tm utc;
    if (gmtime_r(&when, &utc) == NULL) {
        return -1;
    }
    const size_t written = strftime(out, out_len, "%Y-%m-%dT%H:%M:%SZ", &utc);
    return written == 0 ? -1 : (int)written;
}

// Appends ,"name":value with a fixed number of decimals. Returns the new
// length, or -1 on overflow.
static int append_reading(char *out, size_t out_len, int used, bool *first, const char *name,
                          double value, int decimals)
{
    if (used < 0) {
        return -1;
    }
    const int n = bounded(out + used, out_len - (size_t)used, "%s\"%s\":%.*f", *first ? "" : ",", name,
                          decimals, value);
    *first = false;
    return n < 0 ? -1 : used + n;
}

int tc_format_telemetry(const tc_identity_t *id, const char *fw_version, const tc_sample_t *sample,
                        char *out, size_t out_len)
{
    // A NaN or infinity would be refused by the platform and worse, would
    // poison every average downstream if it were not. A disconnected BME280
    // reads as garbage rather than failing, so the check belongs here.
    const bool environment = sample->has_environment && isfinite(sample->temperature_c) &&
                             isfinite(sample->humidity_pct) && isfinite(sample->pressure_hpa);
    const bool battery = sample->has_battery && isfinite(sample->battery_v);

    if (!environment && !battery && !sample->has_rssi) {
        return -1;
    }

    char ts[32];
    if (tc_format_iso8601(sample->timestamp, ts, sizeof ts) < 0) {
        return -1;
    }

    int used = bounded(out, out_len,
                       "{\"schema_version\":\"%s\",\"device_id\":\"%s\",\"site_id\":\"%s\","
                       "\"seq\":%lu,\"ts\":\"%s\",\"fw_version\":\"%s\",\"uptime_s\":%lu,"
                       "\"status\":\"%s\",\"readings\":{",
                       TC_SCHEMA_VERSION, id->device_id, id->site_id, (unsigned long)sample->seq, ts,
                       fw_version, (unsigned long)sample->uptime_s, environment ? "ok" : "fault");

    // Metric names exactly as in the platform's catalogue. A typo here is a
    // metric nothing queries and no rule evaluates and nothing complains.
    bool first = true;
    if (environment) {
        used = append_reading(out, out_len, used, &first, "temperature_c", sample->temperature_c, 2);
        used = append_reading(out, out_len, used, &first, "humidity_pct", sample->humidity_pct, 2);
        used = append_reading(out, out_len, used, &first, "pressure_hpa", sample->pressure_hpa, 2);
    }
    if (battery) {
        used = append_reading(out, out_len, used, &first, "battery_v", sample->battery_v, 3);
    }
    if (sample->has_rssi) {
        used = append_reading(out, out_len, used, &first, "rssi_dbm", (double)sample->rssi_dbm, 0);
    }
    if (used < 0) {
        return -1;
    }

    const int tail = bounded(out + used, out_len - (size_t)used, "}}");
    return tail < 0 ? -1 : used + tail;
}

// Writes a JSON string body (without quotes), escaping as needed and stopping
// at REASON_MAX source characters. Returns characters written or -1.
static int escape_json(const char *in, char *out, size_t out_len)
{
    size_t o = 0;
    for (size_t i = 0; in[i] != '\0' && i < REASON_MAX; i++) {
        const unsigned char c = (unsigned char)in[i];
        char esc[8];
        size_t n;
        if (c == '"' || c == '\\') {
            esc[0] = '\\';
            esc[1] = (char)c;
            n = 2;
        } else if (c < 0x20) {
            n = (size_t)snprintf(esc, sizeof esc, "\\u%04x", c);
        } else {
            esc[0] = (char)c;
            n = 1;
        }
        if (o + n >= out_len) {
            return -1;
        }
        memcpy(out + o, esc, n);
        o += n;
    }
    out[o] = '\0';
    return (int)o;
}

int tc_format_status(const tc_identity_t *id, const char *fw_version, time_t when, const char *event,
                     const char *reason, char *out, size_t out_len)
{
    char ts[32];
    if (tc_format_iso8601(when, ts, sizeof ts) < 0) {
        return -1;
    }

    int used = bounded(out, out_len,
                       "{\"schema_version\":\"%s\",\"device_id\":\"%s\",\"site_id\":\"%s\","
                       "\"ts\":\"%s\",\"event\":\"%s\",\"fw_version\":\"%s\"",
                       TC_SCHEMA_VERSION, id->device_id, id->site_id, ts, event, fw_version);
    if (used < 0) {
        return -1;
    }

    if (reason != NULL) {
        char escaped[REASON_MAX * 6 + 1];
        if (escape_json(reason, escaped, sizeof escaped) < 0) {
            return -1;
        }
        const int n = bounded(out + used, out_len - (size_t)used, ",\"reason\":\"%s\"", escaped);
        if (n < 0) {
            return -1;
        }
        used += n;
    }

    const int tail = bounded(out + used, out_len - (size_t)used, "}");
    return tail < 0 ? -1 : used + tail;
}
