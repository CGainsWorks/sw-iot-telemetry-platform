// The MQTT session: mutual TLS to the broker, birth and last will, commands.

#pragma once

#include <stdbool.h>

#include "esp_err.h"
#include "provisioning.h"

// Starts the client. boot_reason is sent with the birth message; NULL for none.
esp_err_t link_start(const provision_t *p, const char *fw_version, bool first_boot, const char *boot_reason);

bool link_connected(void);

// QoS 1 publish of a ready-made telemetry payload. False if the link is down
// or the client refused it, in which case the caller keeps the sample.
bool link_publish_telemetry(const char *payload, int len);

// Graceful shutdown before deep sleep or a commanded reboot: publishes an
// offline status first, so the broker discards the will and the platform does
// not raise an alert for a device that meant to go quiet.
void link_shutdown(const char *reason);
