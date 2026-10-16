// Environmental sensor and battery voltage.
//
// On a board: a BME280 over I2C and a battery divider on an ADC pin. In the
// QEMU build (CONFIG_TELEMETRY_SENSOR_SIMULATED) there is no I2C device to
// talk to, so the environmental readings come from tc_simulated_environment
// and the battery is not reported. The device announces that in its boot status
// message, so a simulated reading can never be mistaken for a measured one.

#pragma once

#include <stdbool.h>

#include "esp_err.h"
#include "telemetry_core.h"

esp_err_t sensors_init(void);

// Fills the environment and battery fields of a sample.
void sensors_read(tc_sample_t *sample);

// "bme280", "simulated", or "absent".
const char *sensors_source(void);
