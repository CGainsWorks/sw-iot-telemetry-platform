#include "link.h"

#include <stdio.h>
#include <string.h>
#include <time.h>

#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include "freertos/task.h"
#include "mqtt_client.h"
#include "sdkconfig.h"

static const char *TAG = "link";

#define CONNECTED_BIT BIT0
#define DISCONNECTED_BIT BIT1

// Reconnect backoff, doubling between the two. A node hammering a broker it
// cannot reach wastes its own battery and, across a fleet, keeps the broker busy
// refusing handshakes.
#define BACKOFF_START_MS 1000
#define BACKOFF_MAX_MS 60000

static esp_mqtt_client_handle_t s_client;
static EventGroupHandle_t s_events;
static const provision_t *s_prov;
static const char *s_fw_version;
static const char *s_boot_reason;
static bool s_first_boot;
static bool s_birth_sent;
static tc_backoff_t s_backoff;

static char s_telemetry_topic[256];
static char s_status_topic[256];
static char s_command_topic[256];
static char s_will[512];

static void publish_status(const char *event, const char *reason)
{
    char payload[512];
    const int len =
        tc_format_status(&s_prov->identity, s_fw_version, time(NULL), event, reason, payload, sizeof payload);
    if (len > 0) {
        // Retained, so a dashboard that starts later still learns the state.
        esp_mqtt_client_publish(s_client, s_status_topic, payload, len, 1, 1);
    }
}

static void identify_task(void *arg)
{
    (void)arg;
#if CONFIG_TELEMETRY_STATUS_LED_GPIO >= 0
    gpio_reset_pin(CONFIG_TELEMETRY_STATUS_LED_GPIO);
    gpio_set_direction(CONFIG_TELEMETRY_STATUS_LED_GPIO, GPIO_MODE_OUTPUT);
    for (int i = 0; i < 10; i++) {
        gpio_set_level(CONFIG_TELEMETRY_STATUS_LED_GPIO, (uint32_t)(i % 2));
        vTaskDelay(pdMS_TO_TICKS(150));
    }
    gpio_set_level(CONFIG_TELEMETRY_STATUS_LED_GPIO, 0);
#endif
    vTaskDelete(NULL);
}

// The client cannot be stopped from inside its own event handler, so a
// commanded reboot hands over to a task of its own.
static void reboot_task(void *arg)
{
    (void)arg;
    link_shutdown("reboot command");
    esp_restart();
}

static void on_command(const char *data, int len)
{
    switch (tc_parse_command(data, (size_t)len)) {
    case TC_CMD_REBOOT:
        ESP_LOGW(TAG, "command: reboot");
        xTaskCreate(reboot_task, "reboot", 3072, NULL, 5, NULL);
        break;
    case TC_CMD_IDENTIFY:
        // Blinks the status LED so an engineer can find this node on a wall of
        // identical boxes. In its own task so the MQTT task is not held up.
        ESP_LOGI(TAG, "command: identify");
        xTaskCreate(identify_task, "identify", 2048, NULL, 3, NULL);
        break;
    case TC_CMD_UNKNOWN:
        ESP_LOGW(TAG, "command: unknown action, ignored");
        break;
    case TC_CMD_INVALID:
    default:
        ESP_LOGW(TAG, "command: undecodable payload, ignored");
        break;
    }
}

static void on_mqtt_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    (void)base;
    const esp_mqtt_event_handle_t event = (esp_mqtt_event_handle_t)data;

    switch ((esp_mqtt_event_id_t)id) {
    case MQTT_EVENT_CONNECTED:
        ESP_LOGI(TAG, "connected to broker as %s", s_prov->identity.device_id);
        tc_backoff_reset(&s_backoff);
        // Birth message: boot on the first connection after power-on, online
        // on every reconnection after that.
        publish_status(s_first_boot && !s_birth_sent ? "boot" : "online", s_birth_sent ? NULL : s_boot_reason);
        s_birth_sent = true;
        esp_mqtt_client_subscribe(s_client, s_command_topic, 1);
        xEventGroupClearBits(s_events, DISCONNECTED_BIT);
        xEventGroupSetBits(s_events, CONNECTED_BIT);
        break;

    case MQTT_EVENT_DISCONNECTED:
        ESP_LOGW(TAG, "disconnected from broker");
        xEventGroupClearBits(s_events, CONNECTED_BIT);
        xEventGroupSetBits(s_events, DISCONNECTED_BIT);
        break;

    case MQTT_EVENT_DATA:
        // Only the command topic is subscribed, so anything arriving is a
        // command. A fragmented message would arrive in pieces; commands are a
        // few dozen bytes and never are.
        if (event->current_data_offset == 0 && event->data_len == event->total_data_len) {
            on_command(event->data, event->data_len);
        }
        break;

    case MQTT_EVENT_ERROR:
        if (event->error_handle->error_type == MQTT_ERROR_TYPE_TCP_TRANSPORT) {
            // The useful detail for a refused handshake: a revoked or expired
            // certificate shows up here as a TLS alert from the broker.
            // The TLS stack error is an mbedTLS code, which mbedTLS writes as a
            // negative hex number; the sign varies with how it was captured.
            const int tls_err = event->error_handle->esp_tls_stack_err;
            ESP_LOGE(TAG, "transport error: esp-tls 0x%x, mbedtls -0x%04x, socket errno %d",
                     event->error_handle->esp_tls_last_esp_err, (unsigned)(tls_err < 0 ? -tls_err : tls_err),
                     event->error_handle->esp_transport_sock_errno);
        } else if (event->error_handle->error_type == MQTT_ERROR_TYPE_CONNECTION_REFUSED) {
            ESP_LOGE(TAG, "connection refused by broker, code %d", event->error_handle->connect_return_code);
        } else {
            ESP_LOGE(TAG, "mqtt error type %d", event->error_handle->error_type);
        }
        break;

    default:
        break;
    }
}

// The client's own reconnect uses a fixed interval. This one doubles, so it
// is turned off in the client configuration and done here instead.
static void reconnect_task(void *arg)
{
    (void)arg;
    for (;;) {
        xEventGroupWaitBits(s_events, DISCONNECTED_BIT, pdTRUE, pdFALSE, portMAX_DELAY);
        const uint32_t wait_ms = tc_backoff_next(&s_backoff);
        ESP_LOGI(TAG, "reconnecting in %lu ms", (unsigned long)wait_ms);
        vTaskDelay(pdMS_TO_TICKS(wait_ms));
        if ((xEventGroupGetBits(s_events) & CONNECTED_BIT) == 0) {
            esp_mqtt_client_reconnect(s_client);
        }
    }
}

esp_err_t link_start(const provision_t *p, const char *fw_version, bool first_boot, const char *boot_reason)
{
    s_prov = p;
    s_fw_version = fw_version;
    s_first_boot = first_boot;
    s_boot_reason = boot_reason;
    s_events = xEventGroupCreate();
    tc_backoff_init(&s_backoff, BACKOFF_START_MS, BACKOFF_MAX_MS);

    if (tc_topic_telemetry(&p->identity, s_telemetry_topic, sizeof s_telemetry_topic) < 0 ||
        tc_topic_status(&p->identity, s_status_topic, sizeof s_status_topic) < 0 ||
        tc_topic_command(&p->identity, s_command_topic, sizeof s_command_topic) < 0) {
        return ESP_ERR_INVALID_SIZE;
    }

    // Last will and testament. The broker publishes it if the connection drops
    // without a DISCONNECT, which is the only way the platform can tell a
    // crashed device from a quiet one without waiting out a timeout.
    const int will_len = tc_format_status(&p->identity, fw_version, time(NULL), "offline", "unexpected disconnect",
                                          s_will, sizeof s_will);
    if (will_len < 0) {
        return ESP_ERR_INVALID_SIZE;
    }

    const esp_mqtt_client_config_t config = {
        .broker.address.uri = p->mqtt_uri,
        .broker.verification.certificate = p->ca_pem,
        // When the endpoint is an address rather than the name on the broker's
        // certificate, the name to check is provisioned separately. The check
        // itself is never skipped.
        .broker.verification.common_name = p->broker_cn[0] != '\0' ? p->broker_cn : NULL,
        // The client id is the device id. The local broker does not insist on
        // that but AWS IoT Core's device policy allows iot:Connect only as
        // client/${iot:Connection.Thing.ThingName}, so anything else would work
        // here and be refused in production.
        .credentials.client_id = p->identity.device_id,
        .credentials.authentication.certificate = p->cert_pem,
        .credentials.authentication.key = p->key_pem,
        .session.keepalive = 30,
        .session.last_will.topic = s_status_topic,
        .session.last_will.msg = s_will,
        .session.last_will.msg_len = will_len,
        .session.last_will.qos = 1,
        .session.last_will.retain = 1,
        .network.disable_auto_reconnect = true,
        .network.timeout_ms = 20000,
        .buffer.size = 1024,
    };

    s_client = esp_mqtt_client_init(&config);
    if (s_client == NULL) {
        return ESP_FAIL;
    }
    ESP_ERROR_CHECK(esp_mqtt_client_register_event(s_client, ESP_EVENT_ANY_ID, on_mqtt_event, NULL));
    xTaskCreate(reconnect_task, "reconnect", 3072, NULL, 4, NULL);
    ESP_LOGI(TAG, "connecting to %s", p->mqtt_uri);
    return esp_mqtt_client_start(s_client);
}

bool link_connected(void)
{
    return s_events != NULL && (xEventGroupGetBits(s_events) & CONNECTED_BIT) != 0;
}

bool link_publish_telemetry(const char *payload, int len)
{
    if (!link_connected()) {
        return false;
    }
    // QoS 1: at least once. A redelivery is harmless because the platform
    // deduplicates on (device_id, metric, ts). QoS 0 would lose messages on a
    // marginal link; QoS 2 doubles the round trips for a guarantee the
    // deduplicating writer already provides.
    return esp_mqtt_client_publish(s_client, s_telemetry_topic, payload, len, 1, 0) >= 0;
}

void link_shutdown(const char *reason)
{
    if (link_connected()) {
        publish_status("offline", reason);
        // Give the QoS 1 publish a moment to leave before the DISCONNECT.
        vTaskDelay(pdMS_TO_TICKS(300));
        esp_mqtt_client_disconnect(s_client);
    }
    esp_mqtt_client_stop(s_client);
}
