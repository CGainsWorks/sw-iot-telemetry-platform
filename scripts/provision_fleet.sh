#!/usr/bin/env bash
# Provision the whole local stack's PKI into the shared certs volume.
#
# Runs once, before anything else starts. Idempotent: an existing CA is reused, so
# restarting the stack does not invalidate every certificate the fleet is holding.
#
# Ownership matters here. The certificates live in a volume shared by containers
# that run under different accounts, and private keys stay mode 600, so each
# subtree is handed to the account that needs to read it rather than being made
# world readable.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export CERTS_DIR="${CERTS_DIR:-/certs}"

DEVICE_COUNT="${DEVICE_COUNT:-10}"
DEVICE_PREFIX="${DEVICE_PREFIX:-esp32}"
BROKER_CN="${BROKER_CN:-mosquitto}"

# UID/GID the broker runs as. 1883 is the mosquitto account in the official image.
BROKER_UID="${BROKER_UID:-1883}"
BROKER_GID="${BROKER_GID:-1883}"
# UID/GID the platform services and the simulator run as.
APP_UID="${APP_UID:-10001}"
APP_GID="${APP_GID:-10001}"

log() { printf '[provision] %s\n' "$*" >&2; }

log "provisioning PKI into $CERTS_DIR"

bash "$ROOT/scripts/pki.sh" server "$BROKER_CN"
bash "$ROOT/scripts/pki.sh" service ingest-bridge
bash "$ROOT/scripts/pki.sh" service command-dispatcher
bash "$ROOT/scripts/pki.sh" service bench
bash "$ROOT/scripts/pki.sh" service broker-health
bash "$ROOT/scripts/pki.sh" fleet "$DEVICE_COUNT" "$DEVICE_PREFIX"

# Mosquitto refuses to start when crlfile points at a missing file, so the CRL is
# always generated even when nothing has been revoked yet.
bash "$ROOT/scripts/pki.sh" crl

log "setting ownership"
# The CA certificate and the revocation list are public by design: every client
# needs the first to verify the broker, and the broker needs the second.
chmod 644 "$CERTS_DIR/ca/ca.crt" "$CERTS_DIR/ca/ca.crl"
# The CA private key is the root of trust and nothing else ever reads it.
chmod 600 "$CERTS_DIR/ca/ca.key"
chown 0:0 "$CERTS_DIR/ca/ca.key"

chown -R "$BROKER_UID:$BROKER_GID" "$CERTS_DIR/server"
chmod 600 "$CERTS_DIR/server/$BROKER_CN.key"

chown -R "$APP_UID:$APP_GID" "$CERTS_DIR/devices" "$CERTS_DIR/services"
# The healthcheck runs inside the broker container, so its key belongs to the
# broker account rather than to the application account.
chown "$BROKER_UID:$BROKER_GID" "$CERTS_DIR/services/broker-health.crt" "$CERTS_DIR/services/broker-health.key"

# The broker also has to read the CA certificate and the CRL out of ca/.
chmod 755 "$CERTS_DIR" "$CERTS_DIR/ca"

log "done: 1 CA, 1 broker certificate, 4 service certificates, $DEVICE_COUNT devices"
