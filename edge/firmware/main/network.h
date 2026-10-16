// The IP link: Wi-Fi station on a board, or the OpenCores Ethernet MAC that
// Espressif's QEMU emulates. Selected at build time in menuconfig; everything
// above this file is the same either way.

#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"
#include "freertos/FreeRTOS.h"
#include "provisioning.h"

esp_err_t network_start(const provision_t *p);

// Blocks until the interface has an IPv4 address, or the timeout passes.
bool network_wait_ip(TickType_t timeout);

// Received signal strength, when the link has one. Ethernet does not.
bool network_rssi(int32_t *rssi_dbm);

// Short description for logs and the boot status message.
const char *network_kind(void);
