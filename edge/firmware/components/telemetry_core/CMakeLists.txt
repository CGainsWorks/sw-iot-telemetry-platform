idf_component_register(
    SRCS "messages.c" "buffer.c" "bme280_compensation.c"
    INCLUDE_DIRS "include"
)

target_compile_options(${COMPONENT_LIB} PRIVATE -Wall -Wextra -Werror -Wshadow)
