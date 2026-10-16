# Host tests for components/telemetry_core.
#
#   make -C edge/firmware/test/host
#
# Built with the host compiler and both sanitizers, from the same sources the
# firmware links, so an out-of-bounds write in the JSON formatting fails here
# rather than as a corrupted heap on a device.

CORE    := ../../components/telemetry_core
CC      ?= cc
CFLAGS  ?= -std=c11 -O1 -g -Wall -Wextra -Wpedantic -Werror -Wshadow -Wconversion -Wno-sign-conversion
SANITIZE ?= -fsanitize=address,undefined -fno-omit-frame-pointer -fno-sanitize-recover=all
SRCS    := test_telemetry_core.c $(CORE)/messages.c $(CORE)/buffer.c $(CORE)/bme280_compensation.c
BIN     := test_telemetry_core

.PHONY: test clean
test: $(BIN)
	./$(BIN)

$(BIN): $(SRCS) $(CORE)/include/telemetry_core.h
	$(CC) $(CFLAGS) $(SANITIZE) -D_POSIX_C_SOURCE=200809L -I$(CORE)/include -o $@ $(SRCS) -lm

clean:
	rm -f $(BIN)
