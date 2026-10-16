#include "sensors.h"

#include <math.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "sdkconfig.h"

#if !CONFIG_TELEMETRY_SENSOR_SIMULATED
#include "driver/i2c_master.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#endif

#if CONFIG_TELEMETRY_BATTERY_ADC
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"
#include "esp_adc/adc_oneshot.h"
#endif

static const char *TAG = "sensors";

// --- Environment -----------------------------------------------------------

#if CONFIG_TELEMETRY_SENSOR_SIMULATED

static esp_err_t environment_init(void)
{
    ESP_LOGW(TAG, "environmental readings are SIMULATED (no I2C sensor in this build)");
    return ESP_OK;
}

static bool environment_read(tc_sample_t *s)
{
    tc_simulated_environment(s->uptime_s, &s->temperature_c, &s->humidity_pct, &s->pressure_hpa);
    return true;
}

const char *sensors_source(void)
{
    return "simulated";
}

#else  // BME280

#define BME280_REG_CALIB1 0x88
#define BME280_REG_ID 0xD0
#define BME280_REG_RESET 0xE0
#define BME280_REG_CALIB2 0xE1
#define BME280_REG_CTRL_HUM 0xF2
#define BME280_REG_STATUS 0xF3
#define BME280_REG_CTRL_MEAS 0xF4
#define BME280_REG_CONFIG 0xF5
#define BME280_REG_DATA 0xF7
#define BME280_CHIP_ID 0x60
#define I2C_TIMEOUT_MS 50

static i2c_master_bus_handle_t s_bus;
static i2c_master_dev_handle_t s_dev;
static tc_bme280_calib_t s_cal;
static bool s_present;

static esp_err_t reg_read(uint8_t reg, uint8_t *out, size_t len)
{
    return i2c_master_transmit_receive(s_dev, &reg, 1, out, len, I2C_TIMEOUT_MS);
}

static esp_err_t reg_write(uint8_t reg, uint8_t value)
{
    const uint8_t frame[2] = {reg, value};
    return i2c_master_transmit(s_dev, frame, sizeof frame, I2C_TIMEOUT_MS);
}

static esp_err_t environment_init(void)
{
    const i2c_master_bus_config_t bus_config = {
        .i2c_port = -1,
        .sda_io_num = CONFIG_TELEMETRY_I2C_SDA_GPIO,
        .scl_io_num = CONFIG_TELEMETRY_I2C_SCL_GPIO,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    ESP_ERROR_CHECK(i2c_new_master_bus(&bus_config, &s_bus));

    // 0x76 and 0x77 are both common, depending on how the breakout straps SDO.
    uint16_t address = 0;
    if (i2c_master_probe(s_bus, 0x76, I2C_TIMEOUT_MS) == ESP_OK) {
        address = 0x76;
    } else if (i2c_master_probe(s_bus, 0x77, I2C_TIMEOUT_MS) == ESP_OK) {
        address = 0x77;
    } else {
        // Keep running. A node that reports battery and signal strength with a
        // fault status still tells the platform something; one that refuses to
        // boot tells it nothing.
        ESP_LOGW(TAG, "no BME280 at 0x76 or 0x77, publishing status only");
        return ESP_ERR_NOT_FOUND;
    }

    const i2c_device_config_t dev_config = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = address,
        .scl_speed_hz = 100000,
    };
    ESP_ERROR_CHECK(i2c_master_bus_add_device(s_bus, &dev_config, &s_dev));

    uint8_t id = 0;
    if (reg_read(BME280_REG_ID, &id, 1) != ESP_OK || id != BME280_CHIP_ID) {
        // 0x58 here is a BMP280, which has no humidity channel.
        ESP_LOGW(TAG, "device at 0x%02x has chip id 0x%02x, not a BME280", address, id);
        return ESP_ERR_NOT_FOUND;
    }

    reg_write(BME280_REG_RESET, 0xB6);
    vTaskDelay(pdMS_TO_TICKS(5));

    uint8_t block1[26];
    uint8_t block2[7];
    if (reg_read(BME280_REG_CALIB1, block1, sizeof block1) != ESP_OK ||
        reg_read(BME280_REG_CALIB2, block2, sizeof block2) != ESP_OK) {
        ESP_LOGW(TAG, "calibration read failed");
        return ESP_FAIL;
    }
    tc_bme280_parse_calib(block1, block2, &s_cal);

    // Forced mode with oversampling: the sensor converts when asked rather than
    // continuously, which matters on a battery and the IIR filter removes the
    // short-term noise that would otherwise dominate a 10 second cadence.
    // Humidity x1, then IIR x4, then temperature x2 and pressure x16. ctrl_hum
    // only takes effect on the following write to ctrl_meas.
    reg_write(BME280_REG_CTRL_HUM, 0x01);
    reg_write(BME280_REG_CONFIG, 0x08);
    s_present = true;
    ESP_LOGI(TAG, "BME280 at 0x%02x ready", address);
    return ESP_OK;
}

static bool environment_read(tc_sample_t *s)
{
    if (!s_present) {
        return false;
    }
    // osrs_t x2 (010), osrs_p x16 (101), forced mode (01).
    if (reg_write(BME280_REG_CTRL_MEAS, (uint8_t)(0x2 << 5 | 0x5 << 2 | 0x1)) != ESP_OK) {
        return false;
    }
    // Worst case conversion time at these settings is about 46 ms.
    vTaskDelay(pdMS_TO_TICKS(50));
    uint8_t status = 0;
    for (int i = 0; i < 10 && reg_read(BME280_REG_STATUS, &status, 1) == ESP_OK && (status & 0x08); i++) {
        vTaskDelay(pdMS_TO_TICKS(5));
    }

    uint8_t raw[8];
    if (reg_read(BME280_REG_DATA, raw, sizeof raw) != ESP_OK) {
        return false;
    }
    int32_t adc_p, adc_t, adc_h, t_fine;
    tc_bme280_parse_raw(raw, &adc_p, &adc_t, &adc_h);
    // 0x80000 is what the registers hold when a channel was skipped or the
    // part has browned out; reporting it would publish a fiction.
    if (adc_t == 0x80000 || adc_p == 0x80000) {
        return false;
    }
    s->temperature_c = (float)tc_bme280_temperature(&s_cal, adc_t, &t_fine) / 100.0f;
    s->pressure_hpa = (float)tc_bme280_pressure(&s_cal, adc_p, t_fine) / 25600.0f;
    s->humidity_pct = (float)tc_bme280_humidity(&s_cal, adc_h, t_fine) / 1024.0f;
    return isfinite(s->temperature_c) && s->pressure_hpa > 0.0f;
}

const char *sensors_source(void)
{
    return s_present ? "bme280" : "absent";
}

#endif

// --- Battery ---------------------------------------------------------------

#if CONFIG_TELEMETRY_BATTERY_ADC

static adc_oneshot_unit_handle_t s_adc;
static adc_cali_handle_t s_cali;
static bool s_battery_ready;

static void battery_init(void)
{
    const adc_oneshot_unit_init_cfg_t unit = {.unit_id = ADC_UNIT_1};
    if (adc_oneshot_new_unit(&unit, &s_adc) != ESP_OK) {
        return;
    }
    const adc_oneshot_chan_cfg_t channel = {.atten = ADC_ATTEN_DB_12, .bitwidth = ADC_BITWIDTH_DEFAULT};
    if (adc_oneshot_config_channel(s_adc, CONFIG_TELEMETRY_BATTERY_ADC_CHANNEL, &channel) != ESP_OK) {
        return;
    }

    // A calibrated characteristic rather than raw counts times 3.3 V: the ESP32
    // ADC is badly non-linear towards the top of its range, which is exactly
    // where a charged cell sits. Which scheme exists depends on the chip.
    esp_err_t err = ESP_ERR_NOT_SUPPORTED;
#if ADC_CALI_SCHEME_CURVE_FITTING_SUPPORTED
    const adc_cali_curve_fitting_config_t cali = {
        .unit_id = ADC_UNIT_1,
        .chan = CONFIG_TELEMETRY_BATTERY_ADC_CHANNEL,
        .atten = ADC_ATTEN_DB_12,
        .bitwidth = ADC_BITWIDTH_DEFAULT,
    };
    err = adc_cali_create_scheme_curve_fitting(&cali, &s_cali);
#elif ADC_CALI_SCHEME_LINE_FITTING_SUPPORTED
    const adc_cali_line_fitting_config_t cali = {
        .unit_id = ADC_UNIT_1,
        .atten = ADC_ATTEN_DB_12,
        .bitwidth = ADC_BITWIDTH_DEFAULT,
    };
    err = adc_cali_create_scheme_line_fitting(&cali, &s_cali);
#endif
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "no ADC calibration on this chip (%s); battery not reported", esp_err_to_name(err));
        return;
    }
    s_battery_ready = true;
}

static bool battery_read(float *volts)
{
    int mv = 0;
    if (!s_battery_ready ||
        adc_oneshot_get_calibrated_result(s_adc, s_cali, CONFIG_TELEMETRY_BATTERY_ADC_CHANNEL, &mv) != ESP_OK) {
        return false;
    }
    *volts = (float)mv * (float)CONFIG_TELEMETRY_BATTERY_DIVIDER_PERMILLE / 1000.0f / 1000.0f;
    return true;
}

#else

static void battery_init(void)
{
}

static bool battery_read(float *volts)
{
    (void)volts;
    return false;
}

#endif

esp_err_t sensors_init(void)
{
    battery_init();
    return environment_init();
}

void sensors_read(tc_sample_t *sample)
{
    sample->has_environment = environment_read(sample);
    sample->has_battery = battery_read(&sample->battery_v);
}
