#include "provisioning.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_log.h"
#include "mbedtls/oid.h"
#include "mbedtls/x509_crt.h"
#include "nvs.h"

static const char *TAG = "provision";

#define NAMESPACE "provision"

static esp_err_t get_str(nvs_handle_t nvs, const char *key, char *out, size_t out_len, bool required)
{
    size_t len = out_len;
    const esp_err_t err = nvs_get_str(nvs, key, out, &len);
    if (err == ESP_ERR_NVS_NOT_FOUND && !required) {
        out[0] = '\0';
        return ESP_OK;
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "%s: %s", key, esp_err_to_name(err));
    }
    return err;
}

static esp_err_t get_pem(nvs_handle_t nvs, const char *key, char **out)
{
    size_t len = 0;
    esp_err_t err = nvs_get_str(nvs, key, NULL, &len);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "%s: %s", key, esp_err_to_name(err));
        return err;
    }
    *out = malloc(len);
    if (*out == NULL) {
        return ESP_ERR_NO_MEM;
    }
    err = nvs_get_str(nvs, key, *out, &len);
    if (err != ESP_OK) {
        free(*out);
        *out = NULL;
    }
    return err;
}

// Reads the common name, serial and expiry out of the device certificate.
static esp_err_t inspect_certificate(provision_t *p, char *cn, size_t cn_len)
{
    mbedtls_x509_crt crt;
    mbedtls_x509_crt_init(&crt);
    const int rc = mbedtls_x509_crt_parse(&crt, (const unsigned char *)p->cert_pem, strlen(p->cert_pem) + 1);
    if (rc != 0) {
        ESP_LOGE(TAG, "device certificate does not parse (-0x%04x)", (unsigned)-rc);
        mbedtls_x509_crt_free(&crt);
        return ESP_ERR_INVALID_ARG;
    }

    cn[0] = '\0';
    for (const mbedtls_x509_name *name = &crt.subject; name != NULL; name = name->next) {
        if (MBEDTLS_OID_CMP(MBEDTLS_OID_AT_CN, &name->oid) == 0) {
            const size_t n = name->val.len < cn_len - 1 ? name->val.len : cn_len - 1;
            memcpy(cn, name->val.p, n);
            cn[n] = '\0';
        }
    }

    // Leading zero bytes skipped so the form matches `openssl x509 -serial`.
    size_t start = 0;
    while (start + 1 < crt.serial.len && crt.serial.p[start] == 0) {
        start++;
    }
    size_t o = 0;
    for (size_t i = start; i < crt.serial.len && o + 2 < sizeof p->cert_serial; i++) {
        o += (size_t)snprintf(p->cert_serial + o, sizeof p->cert_serial - o, "%02X", crt.serial.p[i]);
    }

    snprintf(p->cert_not_after, sizeof p->cert_not_after, "%04d-%02d-%02dT%02d:%02d:%02dZ", crt.valid_to.year,
             crt.valid_to.mon, crt.valid_to.day, crt.valid_to.hour, crt.valid_to.min, crt.valid_to.sec);

    mbedtls_x509_crt_free(&crt);
    return ESP_OK;
}

esp_err_t provision_load(provision_t *p)
{
    memset(p, 0, sizeof *p);

    nvs_handle_t nvs;
    esp_err_t err = nvs_open(NAMESPACE, NVS_READONLY, &nvs);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "no '%s' namespace in NVS (%s): this board has not been provisioned", NAMESPACE,
                 esp_err_to_name(err));
        return err;
    }

    uint32_t interval = 10;
    if (nvs_get_u32(nvs, "interval_s", &interval) != ESP_OK) {
        interval = 10;
    }
    p->interval_s = interval < 1 ? 1 : interval;

    if ((err = get_str(nvs, "tenant", p->identity.tenant, sizeof p->identity.tenant, true)) != ESP_OK ||
        (err = get_str(nvs, "site_id", p->identity.site_id, sizeof p->identity.site_id, true)) != ESP_OK ||
        (err = get_str(nvs, "device_id", p->identity.device_id, sizeof p->identity.device_id, true)) != ESP_OK ||
        (err = get_str(nvs, "mqtt_uri", p->mqtt_uri, sizeof p->mqtt_uri, true)) != ESP_OK ||
        (err = get_str(nvs, "broker_cn", p->broker_cn, sizeof p->broker_cn, false)) != ESP_OK ||
        (err = get_str(nvs, "ntp_server", p->ntp_server, sizeof p->ntp_server, false)) != ESP_OK ||
        (err = get_str(nvs, "wifi_ssid", p->wifi_ssid, sizeof p->wifi_ssid, false)) != ESP_OK ||
        (err = get_str(nvs, "wifi_pass", p->wifi_pass, sizeof p->wifi_pass, false)) != ESP_OK ||
        (err = get_pem(nvs, "ca_cert", &p->ca_pem)) != ESP_OK ||
        (err = get_pem(nvs, "dev_cert", &p->cert_pem)) != ESP_OK ||
        (err = get_pem(nvs, "dev_key", &p->key_pem)) != ESP_OK) {
        nvs_close(nvs);
        return err;
    }
    nvs_close(nvs);

    if (p->ntp_server[0] == '\0') {
        strcpy(p->ntp_server, "pool.ntp.org");
    }

    if (!tc_identity_valid(&p->identity)) {
        ESP_LOGE(TAG, "tenant, site or device id breaks the topic grammar");
        return ESP_ERR_INVALID_ARG;
    }

    char cn[TC_SEGMENT_MAX];
    if ((err = inspect_certificate(p, cn, sizeof cn)) != ESP_OK) {
        return err;
    }
    if (strcmp(cn, p->identity.device_id) != 0) {
        ESP_LOGE(TAG, "certificate CN '%s' is not the device id '%s'; refusing to start", cn,
                 p->identity.device_id);
        return ESP_ERR_INVALID_STATE;
    }

    ESP_LOGI(TAG, "identity %s at %s/%s, certificate serial %s, not after %s", p->identity.device_id,
             p->identity.tenant, p->identity.site_id, p->cert_serial, p->cert_not_after);
    return ESP_OK;
}
