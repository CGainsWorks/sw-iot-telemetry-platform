#!/usr/bin/env bash
# Build firmware variants with ESP-IDF. Runs inside the pinned espressif/idf
# image (see IDF_IMAGE in the Makefile and the CI workflow):
#
#   bash scripts/build.sh                  every variant
#   bash scripts/build.sh qemu             only the one QEMU boots
#
# Variants:
#   esp32     board build, Xtensa, Wi-Fi and BME280
#   esp32c3   board build, RISC-V, same source
#   qemu      esp32 with OpenCores Ethernet and simulated readings
#
# Each gets its own build directory and sdkconfig, so switching between them
# never reconfigures another.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

build() {
  local variant="$1" target="$2" defaults="$3"
  echo "::group::$variant ($target)"
  idf.py -B "build-$variant" \
    -D IDF_TARGET="$target" \
    -D SDKCONFIG="build-$variant/sdkconfig" \
    -D SDKCONFIG_DEFAULTS="$defaults" \
    build
  idf.py -B "build-$variant" -D SDKCONFIG="build-$variant/sdkconfig" size
  echo "::endgroup::"
}

variants=("$@")
if [[ ${#variants[@]} -eq 0 ]]; then
  variants=(esp32 esp32c3 qemu)
fi

for variant in "${variants[@]}"; do
  case "$variant" in
    esp32)   build esp32 esp32 "sdkconfig.defaults" ;;
    esp32c3) build esp32c3 esp32c3 "sdkconfig.defaults" ;;
    qemu)    build qemu esp32 "sdkconfig.defaults;sdkconfig.qemu" ;;
    *) echo "unknown variant: $variant" >&2; exit 2 ;;
  esac
done
