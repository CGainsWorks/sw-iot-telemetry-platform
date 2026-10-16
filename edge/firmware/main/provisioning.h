// Per-device identity and settings, read from the NVS partition.
//
// Nothing device-specific is compiled into the firmware. One binary serves the
// whole fleet and each board receives its own NVS image at provisioning time:
// identity, broker endpoint and certificates. qemu/run_device.sh builds that
// image from the platform's PKI for the emulated device, which is the same step
// a factory line would perform for a real board.

#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"
#include "telemetry_core.h"

typedef struct {
    tc_identity_t identity;
    char mqtt_uri[128];    // mqtts://host:8883
    char broker_cn[128];   // name to verify the broker certificate against, if not the host
    char ntp_server[64];
    uint32_t interval_s;
    char wifi_ssid[33];
    char wifi_pass[65];
    char *ca_pem;          // heap, NUL terminated
    char *cert_pem;
    char *key_pem;
    char cert_serial[41];  // hex, upper case, no separators
    char cert_not_after[24];
} provision_t;

// Loads and checks everything. Fails if any required key is missing, if a
// segment breaks the topic grammar, or if the certificate common name is not
// the device id: the broker takes the identity from the certificate and ingest
// refuses a payload that disagrees with it, so a mismatch would mean every
// message this device ever sent was dead-lettered.
esp_err_t provision_load(provision_t *out);
