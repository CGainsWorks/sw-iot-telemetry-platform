# ESP32 firmware

An MQTT telemetry node in C on ESP-IDF: BME280 over I2C, battery on an ADC
channel, mutual TLS to the broker, the same message schema and topic grammar as
the simulator.

## Status

| | |
| --- | --- |
| Toolchain | ESP-IDF v5.5.4, in the pinned `espressif/idf:v5.5.4` image |
| Board builds | `esp32` (Xtensa) and `esp32c3` (RISC-V), built in CI on every push |
| Emulated | the `esp32` build, with `sdkconfig.qemu`, booted in Espressif's QEMU (`qemu-system-xtensa`) in CI, publishing into the running platform |
| Host tests | the hardware-independent core, with AddressSanitizer and UBSan |
| On hardware | **never flashed.** There is no board behind this repository |

The emulated device is the strongest evidence here. In CI it is provisioned
from the stack's own PKI, boots, takes an address by DHCP, sets its clock by
SNTP, completes a mutual-TLS handshake with Mosquitto and publishes; the
integration tests then assert that its readings are in TimescaleDB, that ingest
dead-lettered nothing it sent, that it obeys a command sent down the command
topic, that it reconnects with a rotated certificate and that a revoked one is
refused at the handshake. See
[`tests/integration/test_emulated_device.py`](../../tests/integration/test_emulated_device.py).

What QEMU does not emulate and so remains unverified: the Wi-Fi radio, the
BME280, the ADC, power draw and real timing. In the QEMU build the temperature,
humidity and pressure are **simulated** by `tc_simulated_environment`, battery
voltage and signal strength are not reported and the device says so in its
boot status message, which the test asserts.

## Layout

```
components/telemetry_core/   topics, JSON, offline buffer, backoff, command
                             parsing, BME280 compensation. No ESP-IDF headers.
main/                        ESP-IDF glue: NVS provisioning, Wi-Fi or openeth,
                             SNTP, BME280 over I2C, ADC, the MQTT session
test/host/                   host tests for telemetry_core
qemu/                        provision-and-boot script and an SNTP responder
scripts/build.sh             builds every variant inside the IDF image
sdkconfig.defaults           shared settings
sdkconfig.qemu               the QEMU overlay: openeth, simulated readings
partitions.csv               nvs, phy_init, a 1.9 MB factory app
```

## Building and running

```bash
make firmware          # every variant, in the pinned IDF container
make firmware-test     # host tests (needs a C compiler; WSL on Windows)
make device            # provision esp32-qemu-0001 and boot it against the running stack
```

`make device` follows the serial console. Stop it with Ctrl+C.

## Provisioning

Nothing device-specific is compiled in. One binary serves the whole fleet and
each board gets an NVS partition at provisioning time with namespace
`provision`:

| Key | Type | |
| --- | --- | --- |
| `tenant`, `site_id`, `device_id` | string | topic segments; the device id must equal the certificate CN |
| `mqtt_uri` | string | `mqtts://host:8883` |
| `broker_cn` | string, optional | name to verify the broker certificate against when the URI is an address |
| `ntp_server` | string, optional | defaults to `pool.ntp.org` |
| `interval_s` | u32, optional | sample interval, default 10 |
| `wifi_ssid`, `wifi_pass` | string | board builds only |
| `ca_cert`, `dev_cert`, `dev_key` | string | PEM |

[`qemu/run_device.sh`](qemu/run_device.sh) builds that image with ESP-IDF's
`nvs_partition_gen.py` from the certificates `scripts/pki.sh` issued, merges it
into a 4 MB flash image with esptool and boots it. That is the same step a
factory line would perform for a board, with `esptool.py write_flash 0x9000
nvs.bin` in place of QEMU.

At boot the firmware parses its certificate, logs the serial and expiry and
refuses to start if the common name is not its device id: the broker takes the
identity from the certificate and ingest rejects any payload that disagrees, so
a mismatch would mean every message it ever sent was dead-lettered.

The NVS partition is not encrypted in this configuration. A production fleet
would enable flash and NVS encryption, or keep the key in the ESP32's digital
signature peripheral or a secure element, so that a board read out with esptool
does not give up its identity.

## Things in here that are not obvious

**Time before TLS.** A certificate's validity window cannot be checked by a
device that thinks it is 1970 and the ESP32 has no battery-backed clock. SNTP
runs first and no sample is taken until it has succeeded. ESP-IDF ships with
certificate date checking turned off (`CONFIG_MBEDTLS_HAVE_TIME_DATE`); it is
turned on here, or the clock would be decorative.

**The sequence counter lives in RTC memory.** `RTC_DATA_ATTR` survives deep
sleep where an ordinary global does not, because a deep sleep wake is a fresh
boot as far as the firmware is concerned.

**Readings are buffered when the link is down**, 60 of them, drained oldest
first. A node on a marginal link that drops samples is worse than useless,
because the gaps look like the sensor rather than the network.

**The last will is registered at connect time.** The broker publishes it if the
connection drops without a DISCONNECT. A commanded reboot or a deep sleep
publishes its own `offline` first, so a planned restart does not raise an
alert.

**Reconnect backoff doubles from 1 s to 60 s.** ESP-MQTT's own reconnect uses a
fixed interval, so it is disabled and replaced. A fleet hammering an
unreachable broker at a fixed rate keeps it busy refusing handshakes.

**The client id is the device id.** Mosquitto does not care but AWS IoT Core's
device policy only allows `iot:Connect` as `client/${iot:Connection.Thing.ThingName}`.

**BME280 calibration H4 and H5 share a register.** They are 12-bit values split
across the nibbles of 0xE5. The host tests check the parse with negative
values, check the integer compensation against the datasheet's floating point
formulas across the sensor's range and against the worked example in the
Bosch BMP280 datasheet, whose temperature and pressure compensation the BME280
shares.

## Port from the Arduino version

Until October 2026 this was an Arduino sketch built with PlatformIO
(PubSubClient, ArduinoJson, the Adafruit BME280 library), compiled in CI and
never run. It was ported to ESP-IDF so that it could be emulated: Espressif's
QEMU boots ESP-IDF images with the openeth driver and an ESP32 team would
normally be on ESP-IDF anyway for the control it gives over provisioning, TLS
and power. The wire format did not change, which the emulated device test
proves; the firmware version went to 2.0.0.
