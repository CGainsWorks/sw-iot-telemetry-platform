#!/usr/bin/env bash
# Copy the device PKI out of the Compose volume onto the host.
#
# The certificates are generated inside a container into a named volume, so that
# `docker compose up` works on a clean machine with nothing installed but Docker.
# Anything run from the host against the broker still needs them: the benchmark,
# the integration tests, and mosquitto_sub when you are debugging by hand.
#
# certs/ is gitignored. Private keys never leave this machine.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT/certs}"
SERVICE="${CERT_SOURCE_SERVICE:-ingest}"

log() { printf '[certs] %s\n' "$*" >&2; }

if ! docker compose -f "$ROOT/docker-compose.yml" ps --status running --services 2>/dev/null | grep -qx "$SERVICE"; then
  log "error: the '$SERVICE' service is not running."
  log "start the stack first:  docker compose up -d"
  exit 1
fi

mkdir -p "$OUT"

# docker compose cp writes the source directory itself into the destination, so
# copying /certs into a directory that already ends in certs would produce
# certs/certs. Copy the contents instead.
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

docker compose -f "$ROOT/docker-compose.yml" cp "$SERVICE:/certs/." "$tmp" >/dev/null
cp -r "$tmp/." "$OUT/"

# The container runs as uid 10001 and the keys are mode 600, so the copies land
# unreadable by the host account. Ownership is meaningless once they are here.
chmod -R u+rw "$OUT"

log "exported to ${OUT#"$ROOT"/}"
log "  CA:       $(ls "$OUT/ca/ca.crt" >/dev/null 2>&1 && echo present || echo MISSING)"
log "  devices:  $(find "$OUT/devices" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')"
log "  services: $(find "$OUT/services" -name '*.crt' 2>/dev/null | wc -l | tr -d ' ')"
