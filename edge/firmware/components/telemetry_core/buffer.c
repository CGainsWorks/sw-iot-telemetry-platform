// Offline buffer, reconnect backoff and command parsing. See telemetry_core.h.

#include <string.h>

#include "telemetry_core.h"

void tc_buffer_init(tc_buffer_t *buffer, tc_sample_t *storage, size_t capacity)
{
    buffer->slots = storage;
    buffer->capacity = capacity;
    buffer->head = 0;
    buffer->count = 0;
    buffer->dropped = 0;
}

bool tc_buffer_push(tc_buffer_t *buffer, const tc_sample_t *sample)
{
    if (buffer->capacity == 0) {
        buffer->dropped++;
        return false;
    }
    buffer->slots[buffer->head] = *sample;
    buffer->head = (buffer->head + 1) % buffer->capacity;
    if (buffer->count < buffer->capacity) {
        buffer->count++;
        return true;
    }
    buffer->dropped++;
    return false;
}

const tc_sample_t *tc_buffer_oldest(const tc_buffer_t *buffer)
{
    if (buffer->count == 0) {
        return NULL;
    }
    const size_t index = (buffer->head + buffer->capacity - buffer->count) % buffer->capacity;
    return &buffer->slots[index];
}

void tc_buffer_pop(tc_buffer_t *buffer)
{
    if (buffer->count > 0) {
        buffer->count--;
    }
}

size_t tc_buffer_count(const tc_buffer_t *buffer)
{
    return buffer->count;
}

void tc_backoff_init(tc_backoff_t *backoff, uint32_t start_ms, uint32_t max_ms)
{
    backoff->start_ms = start_ms;
    backoff->max_ms = max_ms;
    backoff->current_ms = start_ms;
}

uint32_t tc_backoff_next(tc_backoff_t *backoff)
{
    const uint32_t now = backoff->current_ms;
    // Doubled with an explicit comparison and an overflow guard rather than a
    // min() of mixed types: uint32_t is unsigned long on the RISC-V toolchain
    // and unsigned int on the Xtensa one.
    const uint32_t doubled = now > backoff->max_ms / 2 ? backoff->max_ms : now * 2;
    backoff->current_ms = doubled > backoff->max_ms ? backoff->max_ms : doubled;
    return now;
}

void tc_backoff_reset(tc_backoff_t *backoff)
{
    backoff->current_ms = backoff->start_ms;
}

static size_t skip_space(const char *p, size_t i, size_t len)
{
    while (i < len && (p[i] == ' ' || p[i] == '\t' || p[i] == '\r' || p[i] == '\n')) {
        i++;
    }
    return i;
}

// Scans a JSON string starting at the opening quote. On success returns the
// index just past the closing quote and sets *start and *slen to the raw body.
// Escapes are skipped over, not decoded; no action name contains one.
static size_t scan_string(const char *p, size_t i, size_t len, size_t *start, size_t *slen)
{
    if (i >= len || p[i] != '"') {
        return 0;
    }
    const size_t body = ++i;
    while (i < len && p[i] != '"') {
        if (p[i] == '\\') {
            i++;
        }
        i++;
    }
    if (i >= len) {
        return 0;
    }
    *start = body;
    *slen = i - body;
    return i + 1;
}

// Skips one JSON value of any type at depth zero. Returns the index after it,
// or 0 on malformed input.
static size_t skip_value(const char *p, size_t i, size_t len)
{
    if (i >= len) {
        return 0;
    }
    if (p[i] == '"') {
        size_t s, n;
        return scan_string(p, i, len, &s, &n);
    }
    if (p[i] == '{' || p[i] == '[') {
        int depth = 0;
        while (i < len) {
            if (p[i] == '"') {
                size_t s, n;
                const size_t next = scan_string(p, i, len, &s, &n);
                if (next == 0) {
                    return 0;
                }
                i = next;
                continue;
            }
            if (p[i] == '{' || p[i] == '[') {
                depth++;
            } else if (p[i] == '}' || p[i] == ']') {
                depth--;
                if (depth == 0) {
                    return i + 1;
                }
            }
            i++;
        }
        return 0;
    }
    // A number, true, false or null: runs to the next delimiter.
    while (i < len && p[i] != ',' && p[i] != '}' && p[i] != ']') {
        i++;
    }
    return i;
}

tc_command_t tc_parse_command(const char *payload, size_t len)
{
    if (payload == NULL) {
        return TC_CMD_INVALID;
    }
    size_t i = skip_space(payload, 0, len);
    if (i >= len || payload[i] != '{') {
        return TC_CMD_INVALID;
    }
    i++;

    for (;;) {
        i = skip_space(payload, i, len);
        if (i < len && payload[i] == '}') {
            return TC_CMD_INVALID;  // no action member
        }
        size_t key, key_len;
        i = scan_string(payload, i, len, &key, &key_len);
        if (i == 0) {
            return TC_CMD_INVALID;
        }
        i = skip_space(payload, i, len);
        if (i >= len || payload[i] != ':') {
            return TC_CMD_INVALID;
        }
        i = skip_space(payload, i + 1, len);

        if (key_len == 6 && memcmp(payload + key, "action", 6) == 0) {
            size_t value, value_len;
            if (scan_string(payload, i, len, &value, &value_len) == 0) {
                return TC_CMD_INVALID;  // action present but not a string
            }
            if (value_len == 6 && memcmp(payload + value, "reboot", 6) == 0) {
                return TC_CMD_REBOOT;
            }
            if (value_len == 8 && memcmp(payload + value, "identify", 8) == 0) {
                return TC_CMD_IDENTIFY;
            }
            return TC_CMD_UNKNOWN;
        }

        i = skip_value(payload, i, len);
        if (i == 0) {
            return TC_CMD_INVALID;
        }
        i = skip_space(payload, i, len);
        if (i < len && payload[i] == ',') {
            i++;
            continue;
        }
        return TC_CMD_INVALID;
    }
}
