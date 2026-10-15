# ESP32 firmware

An MQTT telemetry node: BME280 over I2C, mutual TLS to the broker, the same
message schema and topic grammar as the simulator.

## Status

**Compiles for two targets. Never flashed.** There is no hardware behind this
repository.

```
esp32dev   SUCCESS   RAM 15.3%   Flash 72.7% of 1.31 MB
esp32c3    SUCCESS
```

CI verifies both whenever anything under `edge/firmware` changes, and only then,
because the xtensa toolchain is around 400 MB to download and paying that on
every push to a Python service would be most of the repository's CI time.

What that proves: it builds against pinned library versions and produces JSON
matching the published schema. What it does not prove: anything about how it
behaves on a board. The unverified list is in
[docs/limitations.md](../../docs/limitations.md#no-hardware) and includes whether
the TLS handshake fits in the default heap, actual power draw, and the ADC
battery characteristic.

## Building

```bash
cp include/config.h.example include/config.h   # then fill it in
make firmware-build                            # in a container
```

With PlatformIO installed locally:

```bash
pio run -e esp32dev
pio run -e esp32dev -t upload
```

`config.h` holds the device's identity and private key. It is gitignored, and
for more than a handful of devices it should not be used at all: provision into
NVS at manufacture, or use AWS IoT fleet provisioning so the key is generated on
the device and never leaves it. See
[docs/provisioning.md](../../docs/provisioning.md).

## Four things in here that are not obvious

**Time before TLS.** A certificate's validity window cannot be checked by a
device that thinks it is 1970, and the ESP32 has no battery-backed clock. NTP
runs first, and a failure stops the handshake rather than letting it proceed to
a confusing TLS error.

**The sequence counter lives in RTC memory.** `RTC_DATA_ATTR` survives deep
sleep where an ordinary global does not, because a deep sleep wake is a fresh
boot as far as the firmware is concerned. Without it every wake restarts at zero
and the platform cannot tell a reboot from a replay.

**Readings are buffered when the link is down**, 60 of them, oldest first on
drain. A node on a marginal link that drops samples is worse than useless,
because the gaps look like the sensor rather than the network.

**The last will is registered at connect time.** The broker publishes it if the
connection drops without a DISCONNECT, which is the only way to tell a crashed
device from a quiet one without waiting out a timeout. A graceful shutdown or a
deep sleep publishes its own `offline` first, so a planned restart does not
raise an alert.

## Two portability traps, both found by compiling for both targets

`uint32_t` is `unsigned long` on the RISC-V toolchain and `unsigned int` on the
xtensa one. So `min(backoff_ms * 2, 60000U)` deduces conflicting template
argument types and fails on the C3 while building fine on the ESP32. It is now
an explicit comparison. The same promotion affects `%u` in a format string,
hence the casts at each call site.

`LED_BUILTIN` is only defined for boards whose variant file declares it, and the
generic `esp32dev` target does not, so using it fails on exactly the board most
people have. There is a `STATUS_LED_PIN` in the config instead.

Neither would have been caught by building one target.

## Buffer and timing choices

| Setting | Value | Why |
| --- | --- | --- |
| `MQTT_MAX_PACKET_SIZE` | 1024 | PubSubClient defaults to 256 and **silently truncates** a larger publish rather than failing it. A six-reading message is about 320 bytes, so the default would drop every message. |
| `MQTT_SOCKET_TIMEOUT` | 5 s | the default of 15 stalls the main loop on a bad link |
| `BUFFER_CAPACITY` | 60 | 2.4 KB, ten minutes at a 10 second cadence |
| Backoff | 1 s to 60 s | a node hammering an unreachable broker wastes its battery and, across a fleet, keeps the broker busy refusing handshakes |
| `USE_DEEP_SLEEP` | 0 | below roughly a 30 second interval the Wi-Fi and TLS handshake costs more than staying awake. That is arithmetic, not measurement. |
