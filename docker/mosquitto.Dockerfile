# Mosquitto with the platform's configuration baked in.
#
# The alternative, bind-mounting the config from the host, leaves the files owned
# by whoever happens to own the checkout and world readable. Mosquitto 2.0 warns
# about both and future versions refuse to start. Copying them in at build time
# with the right owner and mode settles it, and the ACL is generated from the
# topic grammar anyway so it is not a file anyone edits by hand.
FROM eclipse-mosquitto:2.0.20

COPY --chown=mosquitto:mosquitto --chmod=0700 \
     config/mosquitto/mosquitto.conf /mosquitto/config/mosquitto.conf
COPY --chown=mosquitto:mosquitto --chmod=0700 \
     config/mosquitto/acl /mosquitto/config/acl

USER mosquitto
