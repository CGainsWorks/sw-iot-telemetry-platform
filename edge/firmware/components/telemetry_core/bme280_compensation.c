// BME280 calibration parsing and compensation, from the Bosch BME280 datasheet
// (section 4.2.2 for the register map, 4.2.3 for the integer formulas). Pure
// arithmetic, so test/host checks it against the datasheet's floating point
// formulas and its worked example without any hardware.

#include <math.h>

#include "telemetry_core.h"

static uint16_t u16le(const uint8_t *p)
{
    return (uint16_t)((uint16_t)p[1] << 8 | p[0]);
}

static int16_t s16le(const uint8_t *p)
{
    return (int16_t)u16le(p);
}

void tc_bme280_parse_calib(const uint8_t block1[26], const uint8_t block2[7], tc_bme280_calib_t *out)
{
    // block1 starts at register 0x88.
    out->dig_T1 = u16le(&block1[0]);
    out->dig_T2 = s16le(&block1[2]);
    out->dig_T3 = s16le(&block1[4]);
    out->dig_P1 = u16le(&block1[6]);
    out->dig_P2 = s16le(&block1[8]);
    out->dig_P3 = s16le(&block1[10]);
    out->dig_P4 = s16le(&block1[12]);
    out->dig_P5 = s16le(&block1[14]);
    out->dig_P6 = s16le(&block1[16]);
    out->dig_P7 = s16le(&block1[18]);
    out->dig_P8 = s16le(&block1[20]);
    out->dig_P9 = s16le(&block1[22]);
    // 0xA0 is reserved; dig_H1 is at 0xA1.
    out->dig_H1 = block1[25];

    // block2 starts at register 0xE1. H4 and H5 are 12-bit values sharing the
    // nibbles of 0xE5, which is the part every hand-written driver gets wrong.
    out->dig_H2 = s16le(&block2[0]);
    out->dig_H3 = block2[2];
    out->dig_H4 = (int16_t)((int16_t)(int8_t)block2[3] * 16 | (block2[4] & 0x0F));
    out->dig_H5 = (int16_t)((int16_t)(int8_t)block2[5] * 16 | (block2[4] >> 4));
    out->dig_H6 = (int8_t)block2[6];
}

void tc_bme280_parse_raw(const uint8_t data[8], int32_t *adc_p, int32_t *adc_t, int32_t *adc_h)
{
    *adc_p = (int32_t)((uint32_t)data[0] << 12 | (uint32_t)data[1] << 4 | (uint32_t)data[2] >> 4);
    *adc_t = (int32_t)((uint32_t)data[3] << 12 | (uint32_t)data[4] << 4 | (uint32_t)data[5] >> 4);
    *adc_h = (int32_t)((uint32_t)data[6] << 8 | (uint32_t)data[7]);
}

int32_t tc_bme280_temperature(const tc_bme280_calib_t *cal, int32_t adc_t, int32_t *t_fine)
{
    const int32_t var1 = ((((adc_t >> 3) - ((int32_t)cal->dig_T1 << 1))) * ((int32_t)cal->dig_T2)) >> 11;
    const int32_t var2 = (((((adc_t >> 4) - ((int32_t)cal->dig_T1)) * ((adc_t >> 4) - ((int32_t)cal->dig_T1))) >> 12) *
                          ((int32_t)cal->dig_T3)) >>
                         14;
    *t_fine = var1 + var2;
    return (*t_fine * 5 + 128) >> 8;
}

uint32_t tc_bme280_pressure(const tc_bme280_calib_t *cal, int32_t adc_p, int32_t t_fine)
{
    int64_t var1 = (int64_t)t_fine - 128000;
    int64_t var2 = var1 * var1 * (int64_t)cal->dig_P6;
    var2 = var2 + ((var1 * (int64_t)cal->dig_P5) * 131072);
    var2 = var2 + ((int64_t)cal->dig_P4 * 34359738368LL);
    var1 = ((var1 * var1 * (int64_t)cal->dig_P3) / 256) + ((var1 * (int64_t)cal->dig_P2) * 4096);
    var1 = ((((int64_t)1) * 140737488355328LL) + var1) * ((int64_t)cal->dig_P1) / 8589934592LL;
    if (var1 == 0) {
        return 0;  // avoid a division by zero on a blank calibration
    }
    int64_t p = 1048576 - adc_p;
    p = (((p * 2147483648LL) - var2) * 3125) / var1;
    var1 = ((int64_t)cal->dig_P9 * (p / 8192) * (p / 8192)) / 33554432;
    var2 = ((int64_t)cal->dig_P8 * p) / 524288;
    p = ((p + var1 + var2) / 256) + ((int64_t)cal->dig_P7 * 16);
    return (uint32_t)p;
}

uint32_t tc_bme280_humidity(const tc_bme280_calib_t *cal, int32_t adc_h, int32_t t_fine)
{
    int32_t v = t_fine - (int32_t)76800;
    // dig_H4 may be negative and left-shifting a negative value is undefined
    // in C, so the shift by 20 is written as the multiplication it stands for.
    v = (((((adc_h << 14) - (((int32_t)cal->dig_H4) * 1048576) -(((int32_t)cal->dig_H5) * v)) + (int32_t)16384) >> 15) *
         (((((((v * ((int32_t)cal->dig_H6)) >> 10) * (((v * ((int32_t)cal->dig_H3)) >> 11) + (int32_t)32768)) >> 10) +
            (int32_t)2097152) *
               ((int32_t)cal->dig_H2) +
           8192) >>
          14));
    v = v - (((((v >> 15) * (v >> 15)) >> 7) * ((int32_t)cal->dig_H1)) >> 4);
    v = v < 0 ? 0 : v;
    v = v > 419430400 ? 419430400 : v;
    return (uint32_t)(v >> 12);
}

void tc_simulated_environment(uint32_t uptime_s, float *temperature_c, float *humidity_pct,
                              float *pressure_hpa)
{
    // Three slow sinusoids with unrelated periods, so the readings move and
    // never repeat in step. Arbitrary synthetic shapes, not a model of any room.
    const double two_pi = 6.283185307179586;
    const double t = (double)uptime_s;
    *temperature_c = (float)(21.0 + 1.5 * sin(two_pi * t / 600.0));
    *humidity_pct = (float)(45.0 + 5.0 * sin(two_pi * t / 900.0 + 1.0));
    *pressure_hpa = (float)(1013.25 + 0.8 * sin(two_pi * t / 1800.0));
}
