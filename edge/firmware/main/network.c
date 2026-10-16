#include "network.h"

#include <string.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "freertos/event_groups.h"
#include "sdkconfig.h"

#if CONFIG_TELEMETRY_NET_OPENETH
#include "esp_eth.h"
#else
#include "esp_wifi.h"
#endif

static const char *TAG = "net";

#define GOT_IP_BIT BIT0

static EventGroupHandle_t s_events;

static void on_ip_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    (void)base;
    if (id == IP_EVENT_STA_GOT_IP || id == IP_EVENT_ETH_GOT_IP) {
        const ip_event_got_ip_t *event = (const ip_event_got_ip_t *)data;
        ESP_LOGI(TAG, "address " IPSTR, IP2STR(&event->ip_info.ip));
        xEventGroupSetBits(s_events, GOT_IP_BIT);
    } else if (id == IP_EVENT_STA_LOST_IP) {
        ESP_LOGW(TAG, "address lost");
        xEventGroupClearBits(s_events, GOT_IP_BIT);
    }
}

#if CONFIG_TELEMETRY_NET_OPENETH

const char *network_kind(void)
{
    return "openeth";
}

esp_err_t network_start(const provision_t *p)
{
    (void)p;
    s_events = xEventGroupCreate();
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_ETH_GOT_IP, on_ip_event, NULL));

    // The OpenCores MAC is what QEMU emulates for the ESP32 machine, with user
    // mode networking behind it. The PHY driver is a formality: QEMU answers
    // the MDIO reads and a short autonegotiation timeout keeps boot quick.
    eth_mac_config_t mac_config = ETH_MAC_DEFAULT_CONFIG();
    eth_phy_config_t phy_config = ETH_PHY_DEFAULT_CONFIG();
    phy_config.autonego_timeout_ms = 100;
    esp_eth_mac_t *mac = esp_eth_mac_new_openeth(&mac_config);
    esp_eth_phy_t *phy = esp_eth_phy_new_dp83848(&phy_config);

    esp_eth_handle_t handle = NULL;
    esp_eth_config_t config = ETH_DEFAULT_CONFIG(mac, phy);
    ESP_ERROR_CHECK(esp_eth_driver_install(&config, &handle));

    esp_netif_config_t netif_config = ESP_NETIF_DEFAULT_ETH();
    esp_netif_t *netif = esp_netif_new(&netif_config);
    ESP_ERROR_CHECK(esp_netif_attach(netif, esp_eth_new_netif_glue(handle)));
    ESP_ERROR_CHECK(esp_eth_start(handle));
    ESP_LOGI(TAG, "ethernet (openeth) started");
    return ESP_OK;
}

bool network_rssi(int32_t *rssi_dbm)
{
    (void)rssi_dbm;
    return false;
}

#else  // Wi-Fi station

const char *network_kind(void)
{
    return "wifi";
}

static void on_wifi_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    (void)base;
    if (id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (id == WIFI_EVENT_STA_DISCONNECTED) {
        const wifi_event_sta_disconnected_t *event = (const wifi_event_sta_disconnected_t *)data;
        ESP_LOGW(TAG, "disconnected (reason %d), reconnecting", event->reason);
        xEventGroupClearBits(s_events, GOT_IP_BIT);
        // The driver paces its own retries; a tight loop here would only burn
        // the battery faster against an access point that is not there.
        esp_wifi_connect();
    }
}

esp_err_t network_start(const provision_t *p)
{
    if (p->wifi_ssid[0] == '\0') {
        ESP_LOGE(TAG, "no wifi_ssid provisioned");
        return ESP_ERR_INVALID_ARG;
    }

    s_events = xEventGroupCreate();
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));
    // Credentials come from NVS already; letting the driver persist them as
    // well would rewrite flash on every boot for no benefit.
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));

    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_wifi_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_ip_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_LOST_IP, on_ip_event, NULL));

    wifi_config_t config = {0};
    strncpy((char *)config.sta.ssid, p->wifi_ssid, sizeof config.sta.ssid);
    strncpy((char *)config.sta.password, p->wifi_pass, sizeof config.sta.password);
    config.sta.threshold.authmode = p->wifi_pass[0] == '\0' ? WIFI_AUTH_OPEN : WIFI_AUTH_WPA2_PSK;

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &config));
    // Modem sleep between beacons: worth several milliamps on a mains-less node.
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_MIN_MODEM));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_LOGI(TAG, "wifi station started for '%s'", p->wifi_ssid);
    return ESP_OK;
}

bool network_rssi(int32_t *rssi_dbm)
{
    wifi_ap_record_t ap;
    if (esp_wifi_sta_get_ap_info(&ap) != ESP_OK) {
        return false;
    }
    *rssi_dbm = ap.rssi;
    return true;
}

#endif

bool network_wait_ip(TickType_t timeout)
{
    const EventBits_t bits = xEventGroupWaitBits(s_events, GOT_IP_BIT, pdFALSE, pdTRUE, timeout);
    return (bits & GOT_IP_BIT) != 0;
}
