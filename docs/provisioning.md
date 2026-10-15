# Device provisioning and certificate rotation

How a device gets an identity, and how it gets a new one before the old one
expires.

Every command below has been run. The local CA, the issue, the overlapping
rotation and the revocation with CRL regeneration all work and are exercised by
[`scripts/pki.sh`](../scripts/pki.sh). The AWS variants are written from the
documented API and have not been run against a real account; they are marked.

## The trust model

One certificate authority. One X.509 client certificate per device. The common
name is the device id. The broker refuses any connection it cannot chain to the
CA, and then confines that connection to the topic space named after its common
name.

That is the whole mechanism, and it is identical locally and on AWS:

| | Local | AWS IoT Core |
| --- | --- | --- |
| CA | `certs/ca`, created by `pki.sh` | ACM Private CA, or a registered CA |
| Identity source | certificate CN → MQTT username, via `use_identity_as_username` | certificate CN → thing name |
| Authorisation | ACL `pattern write dt/acme/+/%u/telemetry` | one policy with `${iot:Connection.Thing.ThingName}` |
| Revocation | CRL, `crlfile` in the broker config | certificate status set to `INACTIVE` |

The important property in both: **one policy document covers the whole fleet**.
A policy per device does not scale past a few hundred devices, and a single
permissive policy means any compromised device can publish as any other.

## Local: provisioning a fleet

```bash
# CA, broker certificate, service certificates, and N devices.
# Idempotent: an existing CA is reused, so re-running does not invalidate
# certificates the fleet is already holding.
DEVICE_COUNT=50 bash scripts/provision_fleet.sh

# Or one device at a time.
bash scripts/pki.sh device esp32-000051
bash scripts/pki.sh verify esp32-000051
```

In the Compose stack this runs automatically as the `pki` job before the broker
starts, into a shared volume. That is what makes `docker compose up` work on a
clean machine with nothing installed but Docker.

### What `provision_fleet.sh` does about ownership

The certificates live in a volume shared by containers running under different
accounts, and private keys stay mode 600. So each subtree is handed to the
account that needs to read it rather than being made world readable:

| Path | Owner | Mode | Why |
| --- | --- | --- | --- |
| `ca/ca.crt` | root | 644 | public by design, every client needs it |
| `ca/ca.crl` | root | 644 | public, the broker reads it |
| `ca/ca.key` | root | 600 | the root of trust, nothing else ever reads it |
| `server/*` | 1883 | 600 on the key | the `mosquitto` account in the official image |
| `devices/*`, `services/*` | 10001 | 600 on the keys | the account the services run as |

Getting this wrong is the most common way a mutual-TLS stack fails to start,
with an error that says nothing useful.

## AWS: provisioning at scale

**Not run against a real account.** Written from the documented API.

Three approaches, in increasing order of how much you would want them for a real
fleet.

### One at a time, for a handful of devices

```bash
THING=esp32-000001

aws iot create-thing --thing-name "$THING" \
  --thing-type-name telemetry-sensor \
  --attribute-payload "attributes={site_id=site-a,profile=environment}"

# --set-as-active matters. Without it the certificate exists and every
# connection attempt is refused, which looks exactly like a TLS problem.
aws iot create-keys-and-certificate --set-as-active \
  --certificate-pem-outfile "$THING.crt" \
  --public-key-outfile "$THING.public.key" \
  --private-key-outfile "$THING.private.key"

CERT_ARN=$(aws iot list-certificates \
  --query 'certificates[0].certificateArn' --output text)

aws iot attach-policy --policy-name iot-telemetry-dev-device --target "$CERT_ARN"
aws iot attach-thing-principal --thing-name "$THING" --principal "$CERT_ARN"
```

The policy is created by
[`infra/terraform/aws/modules/iot`](../infra/terraform/aws/modules/iot/main.tf)
and is the same document for every device.

### Fleet provisioning by claim, for a real fleet

The problem with the above is that it requires a per-device step with AWS
credentials somewhere in the manufacturing process. Fleet provisioning solves it:
boards ship with a **shared claim certificate** that can do nothing except
exchange itself for a real one, and the first boot does the exchange.

```bash
aws iot create-provisioning-template \
  --template-name telemetry-sensor-claim \
  --provisioning-role-arn "$PROVISIONING_ROLE_ARN" \
  --template-body file://provisioning-template.json \
  --enabled
```

The claim certificate is worth stealing only once and only for one device, and a
pre-provisioning hook can refuse a serial number that is not on the
manufacturing list. This is the approach a real fleet should use, and it is why
`create_iot_ca` defaults to false in the Terraform.

### Just-in-time registration

The device holds a certificate signed by your own CA and IoT Core registers it
on first connection. Appropriate when certificates are injected at manufacture
by a facility that already has a hardware-backed CA.

### Why the Terraform will not create a CA for you

`create_iot_ca` exists and defaults to `false`. Turning it on makes Terraform
generate the CA private key, which puts it in Terraform state in plain text.
**State is not a key store.** It is in a bucket, in a local cache, and in the
memory of whoever last ran a plan.

The production path is ACM Private CA, or an offline hardware-backed CA whose
certificate is registered once by hand. That is a deliberate, audited step and
not something a plan should apply.

## Rotation

A certificate expires. The device has to be awake and reachable to take a new
one, and a battery node on a long duty cycle can take weeks to notice. So
rotation starts early and the two certificates overlap.

### The procedure

```bash
# 1. Issue a replacement. The outgoing certificate is kept under previous/ and
#    stays valid, so the device has something to fall back to.
bash scripts/pki.sh rotate esp32-000001

# 2. Deliver the new certificate and key to the device, out of band: an OTA
#    payload, a command on cmd/{tenant}/{site}/{device}/req, or a technician.

# 3. Confirm the device has actually reconnected with the new certificate.
docker compose exec timescaledb psql -U telemetry -d telemetry -c \
  "SELECT device_id, last_seen, certificate_expires_at FROM devices
   WHERE device_id = 'esp32-000001';"

# 4. Only then revoke the old one. This regenerates the CRL, which the broker
#    reads, so the outgoing certificate stops working immediately.
bash scripts/pki.sh revoke esp32-000001 --previous
```

The order is the point. Revoking before confirming the switch locks the device
out, and a locked-out battery node on a roof is a site visit.

### Why the CA index needs `unique_subject = no`

Because rotation deliberately creates two live certificates with the same
subject, and `openssl ca` refuses to load an index containing a duplicate
subject unless told duplicates are expected. Without that line, `pki.sh revoke`
fails with `Error creating name index`, which is the least helpful error message
in OpenSSL.

### On AWS

```bash
# Create and attach the replacement.
aws iot create-keys-and-certificate --set-as-active \
  --certificate-pem-outfile new.crt --private-key-outfile new.key
aws iot attach-policy --policy-name iot-telemetry-dev-device --target "$NEW_ARN"
aws iot attach-thing-principal --thing-name "$THING" --principal "$NEW_ARN"

# ... deliver, confirm the device has reconnected ...

# Then deactivate the old one. Deactivate rather than delete: a deleted
# certificate cannot be reactivated, so a botched rotation becomes a site visit
# rather than one command.
aws iot update-certificate --certificate-id "$OLD_ID" --new-status INACTIVE
aws iot detach-thing-principal --thing-name "$THING" --principal "$OLD_ARN"
```

A thing can hold several principals, which is what makes the overlap possible.

### Catching it before it matters

The `certificate-expiring` rule fires 30 days out, scoped fleet-wide, notifying
by webhook with a 24 hour renotify. Thirty days because rotation needs the device
awake and a slow duty cycle can eat weeks of that.

```sql
SELECT device_id, site_id, certificate_expires_at,
       EXTRACT(DAY FROM (certificate_expires_at - now())) AS days_left
FROM devices
WHERE certificate_expires_at < now() + INTERVAL '60 days'
ORDER BY certificate_expires_at;
```

`device_health` exposes the same thing as `certificate_days_remaining`, and
[`analytics/queries/fleet_health_daily.sql`](../analytics/queries/fleet_health_daily.sql)
reports `certificates_expiring_soon` per site per day, so the trend is visible
rather than only the current count.

## Revocation

Revoking is the response to a device being compromised, lost or
decommissioned.

```bash
bash scripts/pki.sh revoke esp32-000001     # regenerates the CRL too
```

The broker has `crlfile` configured, so a revoked certificate is refused at the
handshake rather than being left to publish until it expires. Note what this
means operationally: the broker reads the CRL at startup, so in a real
deployment revocation needs either a broker reload or a short CRL refresh
interval. On IoT Core there is no equivalent problem, because setting a
certificate to `INACTIVE` takes effect immediately.

The CRL also has a `Next Update`, 30 days here. An expired CRL makes Mosquitto
refuse **every** connection, which is a fleet-wide outage caused by a cron job
nobody set up. Regenerating it is part of the routine, not an afterthought.

## What a device holds, and what it must never hold

| | On the device | In the repository |
| --- | --- | --- |
| CA certificate | yes, to verify the broker | yes, it is public |
| Its own certificate | yes | no |
| Its own private key | yes, and nowhere else | **never** |
| CA private key | no | **never** |
| Another device's anything | no | no |

`certs/` is gitignored in its entirety, along with `*.pem`, `*.key` and `*.crt`.
`edge/firmware/include/config.h` is gitignored for the same reason, and the
committed `config.h.example` carries placeholders.

For more than a handful of devices, keys should not be in a header file at all:
provision into NVS at manufacture, or use fleet provisioning so the key is
generated on the device and never leaves it.

## Verifying it actually works

```bash
# The certificate chains to the CA, and has the right extensions.
bash scripts/pki.sh verify esp32-000001

# A real mutual-TLS subscribe. Fails if the ACL, the CRL or the chain is wrong.
mosquitto_sub -h localhost -p 8883 \
  --cafile certs/ca/ca.crt \
  --cert certs/devices/esp32-000001/esp32-000001.crt \
  --key certs/devices/esp32-000001/esp32-000001.key \
  -t 'dt/acme/+/esp32-000001/telemetry' -C 1
```

The adversarial cases are in the integration suite rather than left to be
checked by hand: a client with no certificate cannot connect, there is no
plaintext port, a device cannot publish to another device's topic, and a spoofed
payload on its own topic is dead-lettered. See
[`tests/integration/test_end_to_end.py`](../tests/integration/test_end_to_end.py).
