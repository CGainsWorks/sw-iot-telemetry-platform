// ESP32 telemetry node, ESP-IDF.
//
// Publishes the same messages, on the same topics, with the same certificate
// identity as the simulator in edge/simulator. The simulator came first, so
// this firmware had to match a contract that was already proven rather than
// define one nobody had tested.
//
// Two builds from one source tree:
//
//   board  Wi-Fi station, BME280 over I2C, battery on an ADC pin. Built in CI
//          for esp32 and esp32c3. Never flashed: there is no hardware behind
//          this repository.
//   qemu   OpenCores Ethernet and simulated environmental readings, for
//          Espressif's QEMU. Built and booted in CI, where it connects to the
//          platform's broker over mutual TLS and its telemetry is asserted in
//          TimescaleDB. See qemu/run_device.sh.
//
// Four things here are not obvious and matter on real hardware:
//
//   1. Time must be right before TLS. Certificate validity cannot be checked
//      by a device that thinks it is 1970 and the ESP32 has no battery-backed
//      clock. SNTP comes first and no sample is taken until it has succeeded.
//   2. The sequence counter lives in RTC memory so it survives deep sleep.
//      Without that, every wake restarts at zero and the platform cannot tell
//      a reboot from a replay.
//   3. Readings are buffered while the link is down and drained oldest first.
//      A node on a marginal link that drops samples is worse than useless,
//      because the gaps look like the sensor rather than the network.
//   4. The last will is registered at connect time, so an ungraceful
//      disconnect produces an offline event without a timeout.

#include <stdio.h>
#include <string.h>
#include <time.h>

#include "esp_attr.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_netif_sntp.h"
#include "esp_sleep.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"
#include "sdkconfig.h"

#include "link.h"
#include "network.h"
#include "provisioning.h"
#include "sensors.h"
#include "telemetry_core.h"

static const char *TAG = "node";

// Readings held while the link is down. 60 covers ten minutes at a 10 second
// cadence in a few kilobytes. Beyond that the oldest are overwritten.
#define BUFFER_CAPACITY 60

// RTC slow memory keeps its contents through deep sleep, where an ordinary
// global is reinitialised, because a wake is a fresh boot to the firmware.
RTC_DATA_ATTR static uint32_t rtc_sequence;
RTC_DATA_ATTR static uint32_t rtc_boot_count;

static provision_t s_prov;
static tc_sample_t s_storage[BUFFER_CAPACITY];
static tc_buffer_t s_buffer;

static uint32_t uptime_s(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000000);
}

static bool sync_clock(void)
{
    // UTC throughout. A device applying a local offset is a device whose data
    // lands in the wrong bucket twice a year.
    esp_sntp_config_t config = ESP_NETIF_SNTP_DEFAULT_CONFIG(s_prov.ntp_server);
    esp_netif_sntp_init(&config);

    tc_backoff_t backoff;
    tc_backoff_init(&backoff, 2000, 60000);
    while (esp_netif_sntp_sync_wait(pdMS_TO_TICKS(15000)) != ESP_OK || !tc_clock_plausible(time(NULL))) {
        const uint32_t wait_ms = tc_backoff_next(&backoff);
        ESP_LOGW(TAG, "no time from %s yet, retrying in %lu ms", s_prov.ntp_server, (unsigned long)wait_ms);
        vTaskDelay(pdMS_TO_TICKS(wait_ms));
    }
    char now[32];
    tc_format_iso8601(time(NULL), now, sizeof now);
    ESP_LOGI(TAG, "clock set from %s: %s", s_prov.ntp_server, now);
    return true;
}

static tc_sample_t take_sample(void)
{
    tc_sample_t sample;
    memset(&sample, 0, sizeof sample);
    sample.timestamp = time(NULL);
    sample.seq = ++rtc_sequence;
    sample.uptime_s = uptime_s();
    sensors_read(&sample);
    sample.has_rssi = network_rssi(&sample.rssi_dbm);
    return sample;
}

static bool publish_sample(const tc_sample_t *sample)
{
    char payload[512];
    const int len = tc_format_telemetry(&s_prov.identity, CONFIG_TELEMETRY_FW_VERSION, sample, payload, sizeof payload);
    if (len < 0) {
        // Nothing to report at all: no sensor, no battery, no signal. Dropped
        // rather than buffered, because it would never become publishable.
        ESP_LOGW(TAG, "sample %lu has no readings, skipped", (unsigned long)sample->seq);
        return true;
    }
    if (!link_publish_telemetry(payload, len)) {
        return false;
    }
    ESP_LOGI(TAG, "published seq %lu (%s)", (unsigned long)sample->seq, sample->has_environment ? "ok" : "fault");
    return true;
}

static void drain_buffer(void)
{
    const tc_sample_t *oldest;
    while ((oldest = tc_buffer_oldest(&s_buffer)) != NULL && link_connected()) {
        if (!publish_sample(oldest)) {
            return;  // leave the rest buffered rather than dropping them
        }
        tc_buffer_pop(&s_buffer);
    }
}

void app_main(void)
{
    rtc_boot_count++;
    const bool first_boot = esp_sleep_get_wakeup_cause() == ESP_SLEEP_WAKEUP_UNDEFINED;
    ESP_LOGI(TAG, "firmware %s, boot %lu, sequence %lu", CONFIG_TELEMETRY_FW_VERSION, (unsigned long)rtc_boot_count,
             (unsigned long)rtc_sequence);

    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        // Never erased automatically: the partition holds this device's
        // identity and wiping it would turn a recoverable fault into a site
        // visit.
        ESP_LOGE(TAG, "NVS needs erasing (%s); reprovision this board", esp_err_to_name(err));
        return;
    }
    ESP_ERROR_CHECK(err);

    if (provision_load(&s_prov) != ESP_OK) {
        ESP_LOGE(TAG, "not provisioned; halting");
        return;
    }

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    sensors_init();

    char boot_reason[160];
#if CONFIG_TELEMETRY_SENSOR_SIMULATED
    snprintf(boot_reason, sizeof boot_reason, "emulated device: %s on QEMU, %s environmental readings",
             network_kind(), sensors_source());
#else
    snprintf(boot_reason, sizeof boot_reason, "%s, sensor %s", network_kind(), sensors_source());
#endif

    ESP_ERROR_CHECK(network_start(&s_prov));
    while (!network_wait_ip(pdMS_TO_TICKS(30000))) {
        ESP_LOGW(TAG, "still waiting for an address");
    }

    // Order matters: the certificate validity check needs a correct clock, so
    // a failed time sync stops here rather than proceeding to a TLS error that
    // explains nothing.
    sync_clock();

    tc_buffer_init(&s_buffer, s_storage, BUFFER_CAPACITY);
    ESP_ERROR_CHECK(link_start(&s_prov, CONFIG_TELEMETRY_FW_VERSION, first_boot, boot_reason));

    const TickType_t period = pdMS_TO_TICKS(s_prov.interval_s * 1000);
    TickType_t last_wake = xTaskGetTickCount();
    for (;;) {
        const tc_sample_t sample = take_sample();

        if (link_connected()) {
            drain_buffer();
            if (!publish_sample(&sample)) {
                tc_buffer_push(&s_buffer, &sample);
            }
        } else {
            if (!tc_buffer_push(&s_buffer, &sample)) {
                ESP_LOGW(TAG, "buffer full, overwrote the oldest sample");
            }
            ESP_LOGI(TAG, "link down, holding %u samples", (unsigned)tc_buffer_count(&s_buffer));
        }

#if CONFIG_TELEMETRY_DEEP_SLEEP
        // Only once the buffer is clear: the backlog lives in RAM that does not
        // survive deep sleep.
        if (tc_buffer_count(&s_buffer) == 0 && link_connected()) {
            link_shutdown("deep sleep");
            ESP_LOGI(TAG, "deep sleep for %lu s", (unsigned long)s_prov.interval_s);
            esp_deep_sleep((uint64_t)s_prov.interval_s * 1000000ULL);
        }
#endif

        vTaskDelayUntil(&last_wake, period);
    }
}
