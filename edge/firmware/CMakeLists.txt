# ESP-IDF project for the telemetry node.
#
#   idf.py set-target esp32 build                       board build
#   idf.py -D SDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.qemu" build   QEMU build
#
# scripts/build.sh runs every variant inside the pinned espressif/idf image.
cmake_minimum_required(VERSION 3.16)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(telemetry_node)
