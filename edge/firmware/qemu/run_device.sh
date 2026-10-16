#!/usr/bin/env bash
# Provision and boot one emulated ESP32 in Espressif's QEMU.
#
# Runs inside the pinned espressif/idf image, attached to the Compose network
# with the certificate volume mounted, as the esp32-qemu service:
#
#   docker compose --profile emulated-device run --rm --no-deps \
#     -e DEVICE_ID=esp32-qemu-0001 esp32-qemu
#
# What it does, in the order a factory line would:
#
#   1. Builds an NVS partition image holding this device's identity, broker
#      endpoint and certificates, from the platform's own PKI. The firmware
#      binary itself is identical for every device.
#   2. Merges bootloader, partition table, NVS and application into one 4 MB
#      flash image.
#   3. Starts an SNTP responder, because the firmware will not attempt TLS
#      until it knows the time.
#   4. Boots qemu-system-xtensa with an OpenCores Ethernet NIC on user-mode
#      networking. The guest reaches the broker through QEMU's NAT, over the
#      same mutual-TLS listener as every simulated device.
#
# The serial console is this process's stdout, so `docker logs` is the device's
# UART.
set -euo pipefail

DEVICE_ID="${DEVICE_ID:?set DEVICE_ID to a provisioned device}"
SITE_ID="${SITE_ID:-site-q}"
TENANT="${TENANT:-acme}"
BROKER_HOST="${BROKER_HOST:-mosquitto}"
BROKER_PORT="${BROKER_PORT:-8883}"
# The name on the broker's certificate. The device connects by address, because
# the guest has no route to Docker's DNS and verifies the certificate against
# this name instead. Hostname verification is never switched off.
BROKER_CN="${BROKER_CN:-mosquitto}"
INTERVAL_S="${INTERVAL_S:-2}"
CERTS_DIR="${CERTS_DIR:-/certs}"
FIRMWARE_DIR="${FIRMWARE_DIR:-/firmware/build-qemu}"
# "current" boots with the device's live certificate; "previous" with the one
# kept under previous/ by pki.sh rotate, to prove a revoked certificate is
# refused.
CERT_VARIANT="${CERT_VARIANT:-current}"

log() { printf '[qemu-device] %s\n' "$*" >&2; }
die() { log "error: $*"; exit 1; }

device_dir="$CERTS_DIR/devices/$DEVICE_ID"
if [[ "$CERT_VARIANT" == "previous" ]]; then
  cert_dir="$device_dir/previous"
else
  cert_dir="$device_dir"
fi
[[ -f "$cert_dir/$DEVICE_ID.crt" ]] || die "no certificate at $cert_dir; provision $DEVICE_ID first"
[[ -f "$FIRMWARE_DIR/flash_args" ]] || die "no QEMU firmware build at $FIRMWARE_DIR"

broker_ip="$(getent hosts "$BROKER_HOST" | awk '{ print $1; exit }')"
[[ -n "$broker_ip" ]] || die "cannot resolve $BROKER_HOST"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

# --- 1. NVS image -----------------------------------------------------------
# 10.0.2.2 is the host side of QEMU's user-mode network, which inside this
# container is the SNTP responder started below.
{
  printf 'key,type,encoding,value\n'
  printf 'provision,namespace,,\n'
  printf 'tenant,data,string,%s\n' "$TENANT"
  printf 'site_id,data,string,%s\n' "$SITE_ID"
  printf 'device_id,data,string,%s\n' "$DEVICE_ID"
  printf 'mqtt_uri,data,string,mqtts://%s:%s\n' "$broker_ip" "$BROKER_PORT"
  printf 'broker_cn,data,string,%s\n' "$BROKER_CN"
  printf 'ntp_server,data,string,10.0.2.2\n'
  printf 'interval_s,data,u32,%s\n' "$INTERVAL_S"
  printf 'ca_cert,file,string,%s\n' "$CERTS_DIR/ca/ca.crt"
  printf 'dev_cert,file,string,%s\n' "$cert_dir/$DEVICE_ID.crt"
  printf 'dev_key,file,string,%s\n' "$cert_dir/$DEVICE_ID.key"
} > "$work/nvs.csv"

python "$IDF_PATH/components/nvs_flash/nvs_partition_generator/nvs_partition_gen.py" \
  generate "$work/nvs.csv" "$work/nvs.bin" 0x6000 >/dev/null

# --- 2. Flash image ---------------------------------------------------------
# flash_args lists bootloader, partition table and application with their
# offsets, relative to the build directory. NVS goes at 0x9000, matching
# partitions.csv.
(
  cd "$FIRMWARE_DIR"
  esptool.py --chip esp32 merge_bin --fill-flash-size 4MB -o "$work/flash.bin" \
    @flash_args 0x9000 "$work/nvs.bin" >/dev/null
)

# --- 3. Time ----------------------------------------------------------------
python "$(dirname "$0")/sntp_responder.py" --port 123 &

# --- 4. Boot ----------------------------------------------------------------
log "booting $DEVICE_ID ($CERT_VARIANT certificate) against $BROKER_HOST ($broker_ip:$BROKER_PORT)"
# The watchdog timer is disabled because emulation runs slower than silicon and
# a TLS handshake under QEMU can outlast the default timeout.
exec qemu-system-xtensa \
  -nographic \
  -machine esp32 \
  -drive "file=$work/flash.bin,if=mtd,format=raw" \
  -global driver=timer.esp32.timg,property=wdt_disable,value=true \
  -nic user,model=open_eth
