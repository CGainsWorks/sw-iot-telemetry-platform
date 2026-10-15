#!/usr/bin/env bash
# Device PKI for the local stack.
#
# Stands in for the AWS IoT Core certificate authority. The trust model is
# identical: one CA, one X.509 client certificate per device, common name equal
# to the device id, and a broker that refuses any connection it cannot chain to
# the CA. The provisioning and rotation procedures exercised here are therefore
# the same ones documented for production, only the CA is local.
#
# Private keys never leave the certs/ directory and certs/ is gitignored.
set -euo pipefail

# Git Bash on Windows rewrites any argument that looks like a POSIX path into a
# Windows one, which mangles an X.509 subject such as "/O=Acme/CN=device". Excluding
# the subject prefixes keeps the subject intact while leaving real path arguments to
# be converted normally. On Linux the variable is simply ignored.
export MSYS2_ARG_CONV_EXCL='/O=;/OU=;/CN=;/C=;/ST=;/L=;/emailAddress='

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CERTS="${CERTS_DIR:-$ROOT/certs}"
CA_DIR="$CERTS/ca"
DEVICE_DIR="$CERTS/devices"
SERVICE_DIR="$CERTS/services"
SERVER_DIR="$CERTS/server"

CA_DAYS="${CA_DAYS:-3650}"
LEAF_DAYS="${LEAF_DAYS:-365}"
# Validity of the revocation list.
#
# An expired CRL makes Mosquitto refuse *every* connection, not just revoked
# ones, so a stale CRL is a fleet-wide outage caused by a missed scheduled job.
# That happened while developing this repository: the stack was left up, the CRL
# passed its next-update date, and the broker started rejecting the whole fleet
# with "certificate verify failed" while every certificate was still perfectly
# valid.
#
# 90 days here so a demo left running does not quietly break. Production should
# be far shorter, days rather than months, with regeneration automated and
# monitored, because the whole point of a CRL is that a revocation takes effect
# promptly. See docs/provisioning.md.
CRL_DAYS="${CRL_DAYS:-90}"
KEY_BITS="${KEY_BITS:-2048}"
ORG="${PKI_ORG:-CGainsWorks IoT}"

log() { printf '[pki] %s\n' "$*" >&2; }
die() { printf '[pki] error: %s\n' "$*" >&2; exit 1; }

need_openssl() {
  command -v openssl >/dev/null 2>&1 || die "openssl is not on PATH"
}

# Paths inside an openssl config file are read by openssl itself, so MSYS argument
# conversion never sees them and a "/c/Users/..." path fails to resolve on Windows.
# Convert to a native path when cygpath is available; a no-op everywhere else.
native_path() {
  if command -v cygpath >/dev/null 2>&1; then
    cygpath -m "$1"
  else
    printf '%s' "$1"
  fi
}

write_ca_config() {
  # shellcheck disable=SC2016
  # The $dir references below are deliberately literal: they are openssl config
  # file variables, expanded by openssl against the dir setting on the preceding
  # line, not shell variables. Letting the shell expand them would write an
  # empty path into the config and openssl would fail to find its own database.
  {
    printf '[ ca ]\n'
    printf 'default_ca = CA_default\n\n'
    printf '[ CA_default ]\n'
    printf 'dir = %s\n' "$(native_path "$CA_DIR")"
    printf 'database = $dir/index.txt\n'
    printf 'certificate = $dir/ca.crt\n'
    printf 'private_key = $dir/ca.key\n'
    printf 'crlnumber = $dir/crlnumber\n'
    printf 'default_md = sha256\n'
    printf 'default_crl_days = %s\n' "$CRL_DAYS"
  } > "$CA_DIR/openssl.cnf"
}

ext_client() {
  {
    printf 'basicConstraints = critical, CA:FALSE\n'
    printf 'keyUsage = critical, digitalSignature, keyEncipherment\n'
    printf 'extendedKeyUsage = clientAuth\n'
    printf 'subjectAltName = DNS:%s\n' "$1"
  }
}

ext_server() {
  {
    printf 'basicConstraints = critical, CA:FALSE\n'
    printf 'keyUsage = critical, digitalSignature, keyEncipherment\n'
    printf 'extendedKeyUsage = serverAuth\n'
    printf 'subjectAltName = DNS:%s, DNS:localhost, DNS:mosquitto, IP:127.0.0.1\n' "$1"
  }
}

init_ca() {
  mkdir -p "$CA_DIR"
  if [[ -f "$CA_DIR/ca.crt" ]]; then
    return 0
  fi
  log "creating root CA (${CA_DAYS} days)"
  openssl genrsa -out "$CA_DIR/ca.key" "$KEY_BITS" 2>/dev/null
  openssl req -x509 -new -nodes -sha256 \
    -key "$CA_DIR/ca.key" \
    -days "$CA_DAYS" \
    -subj "/O=$ORG/OU=PKI/CN=$ORG Root CA" \
    -out "$CA_DIR/ca.crt" 2>/dev/null
  # Serial and CRL bookkeeping, needed for revocation.
  : > "$CA_DIR/index.txt"
  # Rotation deliberately issues a second live certificate with the same subject
  # so the device has an overlap window. openssl refuses to load an index with a
  # duplicate subject unless told that duplicates are expected.
  echo 'unique_subject = no' > "$CA_DIR/index.txt.attr"
  echo 1000 > "$CA_DIR/serial"
  echo 1000 > "$CA_DIR/crlnumber"
  chmod 600 "$CA_DIR/ca.key"
  write_ca_config
  log "CA fingerprint: $(openssl x509 -in "$CA_DIR/ca.crt" -noout -fingerprint -sha256 | cut -d= -f2)"
}

# issue <out_dir> <common_name> <extension_function>
issue() {
  local out="$1" cn="$2" extfn="$3"
  mkdir -p "$out"
  local ext_file
  ext_file="$(mktemp)"
  "$extfn" "$cn" > "$ext_file"

  openssl genrsa -out "$out/$cn.key" "$KEY_BITS" 2>/dev/null
  openssl req -new -sha256 \
    -key "$out/$cn.key" \
    -subj "/O=$ORG/OU=Devices/CN=$cn" \
    -out "$out/$cn.csr" 2>/dev/null
  openssl x509 -req -sha256 \
    -in "$out/$cn.csr" \
    -CA "$CA_DIR/ca.crt" -CAkey "$CA_DIR/ca.key" \
    -CAcreateserial -CAserial "$CA_DIR/serial" \
    -days "$LEAF_DAYS" \
    -extfile "$ext_file" \
    -out "$out/$cn.crt" 2>/dev/null
  rm -f "$ext_file" "$out/$cn.csr"
  chmod 600 "$out/$cn.key"

  # Record in the CA index so revoke and crl can find it later. The index is a
  # fixed six-column format and openssl refuses to read it unless the expiry is
  # YYMMDDHHMMSSZ and the subject uses the slash-separated compat form.
  local serial subject expiry
  serial="$(openssl x509 -in "$out/$cn.crt" -noout -serial | cut -d= -f2)"
  subject="$(openssl x509 -in "$out/$cn.crt" -noout -subject -nameopt compat | sed 's/^subject= *//')"
  expiry="$(date -u -d "$(openssl x509 -in "$out/$cn.crt" -noout -enddate | cut -d= -f2)" +%y%m%d%H%M%SZ)"
  printf 'V\t%s\t\t%s\tunknown\t%s\n' "$expiry" "$serial" "$subject" >> "$CA_DIR/index.txt"

  log "issued $cn (serial $serial, ${LEAF_DAYS} days)"
}

cmd_server() {
  init_ca
  local cn="${1:-mosquitto}"
  mkdir -p "$SERVER_DIR"
  issue "$SERVER_DIR" "$cn" ext_server
  cp "$CA_DIR/ca.crt" "$SERVER_DIR/ca.crt"
}

cmd_device() {
  init_ca
  local id="${1:?usage: pki.sh device <device-id>}"
  if [[ ! "$id" =~ ^[a-z0-9][a-z0-9-]{2,63}$ ]]; then
    die "device id '$id' does not match the topic segment grammar"
  fi
  local out="$DEVICE_DIR/$id"
  if [[ -f "$out/$id.crt" ]]; then
    return 0
  fi
  issue "$out" "$id" ext_client
  cp "$CA_DIR/ca.crt" "$out/ca.crt"
}

cmd_service() {
  init_ca
  local name="${1:?usage: pki.sh service <name>}"
  mkdir -p "$SERVICE_DIR"
  if [[ -f "$SERVICE_DIR/$name.crt" ]]; then
    return 0
  fi
  issue "$SERVICE_DIR" "$name" ext_client
  cp "$CA_DIR/ca.crt" "$SERVICE_DIR/ca.crt"
}

# Rotation keeps the outgoing certificate on disk under previous/ so a device
# that cannot complete the handshake with the new one can fall back. See
# docs/provisioning.md for the full procedure.
cmd_rotate() {
  init_ca
  local id="${1:?usage: pki.sh rotate <device-id>}"
  local out="$DEVICE_DIR/$id"
  [[ -f "$out/$id.crt" ]] || die "device $id has no certificate to rotate"

  mkdir -p "$out/previous"
  cp "$out/$id.crt" "$out/previous/$id.crt"
  cp "$out/$id.key" "$out/previous/$id.key"
  log "kept outgoing certificate at $out/previous"

  rm -f "$out/$id.crt" "$out/$id.key"
  issue "$out" "$id" ext_client
  log "rotated $id. The outgoing certificate stays valid until: pki.sh revoke $id --previous"
}

cmd_revoke() {
  init_ca
  write_ca_config
  local id="${1:?usage: pki.sh revoke <device-id> [--previous]}"
  local which="${2:-}"
  local target="$DEVICE_DIR/$id/$id.crt"
  if [[ "$which" == "--previous" ]]; then
    target="$DEVICE_DIR/$id/previous/$id.crt"
  fi
  [[ -f "$target" ]] || die "no certificate at $target"

  openssl ca -config "$CA_DIR/openssl.cnf" -revoke "$target" -batch 2>/dev/null \
    || die "revoke failed for $id"
  log "revoked $id ${which}"
  cmd_crl
}

cmd_crl() {
  init_ca
  write_ca_config
  openssl ca -config "$CA_DIR/openssl.cnf" -gencrl -out "$CA_DIR/ca.crl" -batch 2>/dev/null
  mkdir -p "$SERVER_DIR"
  cp "$CA_DIR/ca.crl" "$SERVER_DIR/ca.crl"
  log "wrote CRL to $CA_DIR/ca.crl"
}

cmd_fleet() {
  local count="${1:-10}" prefix="${2:-esp32}"
  init_ca
  local i id
  for ((i = 1; i <= count; i++)); do
    id="$(printf '%s-%06d' "$prefix" "$i")"
    cmd_device "$id"
  done
  log "fleet of $count devices ready under $DEVICE_DIR"
}

cmd_verify() {
  local id="${1:?usage: pki.sh verify <device-id>}"
  openssl verify -CAfile "$CA_DIR/ca.crt" "$DEVICE_DIR/$id/$id.crt"
  openssl x509 -in "$DEVICE_DIR/$id/$id.crt" -noout -subject -dates
}

usage() {
  {
    printf 'usage: scripts/pki.sh <command> [args]\n\n'
    printf '  init-ca                  create the root CA if it does not exist\n'
    printf '  server [cn]              issue the broker server certificate (default cn: mosquitto)\n'
    printf '  device <device-id>       issue a client certificate for one device\n'
    printf '  service <name>           issue a client certificate for a platform service\n'
    printf '  fleet [count] [prefix]   issue certificates for a whole simulated fleet\n'
    printf '  rotate <device-id>       issue a replacement, keeping the old one under previous/\n'
    printf '  revoke <id> [--previous] revoke a certificate and regenerate the CRL\n'
    printf '  crl                      regenerate the certificate revocation list\n'
    printf '  verify <device-id>       check a certificate chains to the CA\n\n'
    printf 'Environment: CERTS_DIR, CA_DAYS, LEAF_DAYS, KEY_BITS, PKI_ORG\n'
  }
}

need_openssl
case "${1:-}" in
  init-ca) shift; init_ca "$@" ;;
  server)  shift; cmd_server "$@" ;;
  device)  shift; cmd_device "$@" ;;
  service) shift; cmd_service "$@" ;;
  fleet)   shift; cmd_fleet "$@" ;;
  rotate)  shift; cmd_rotate "$@" ;;
  revoke)  shift; cmd_revoke "$@" ;;
  crl)     shift; cmd_crl "$@" ;;
  verify)  shift; cmd_verify "$@" ;;
  ""|-h|--help|help) usage ;;
  *) usage; exit 2 ;;
esac
