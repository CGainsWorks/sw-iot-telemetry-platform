idf_component_register(
    SRCS "app_main.c" "provisioning.c" "network.c" "sensors.c" "link.c"
    INCLUDE_DIRS "."
    REQUIRES telemetry_core nvs_flash esp_netif esp_event esp_wifi esp_eth esp_timer mqtt mbedtls
             esp_driver_i2c esp_driver_gpio esp_adc
)

# Warnings are errors in this project's own code. ESP-IDF's components keep their
# own settings.
target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra -Werror)
