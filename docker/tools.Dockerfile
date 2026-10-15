# Helper image for the two one-shot jobs the stack needs before anything else can
# start: issuing the device PKI, and applying the database migrations. Both are
# small, both need to run to completion and exit, and neither belongs inside a
# long-lived service image.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      bash openssl coreutils ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY services/common /app/services/common
RUN pip install --no-cache-dir ./services/common "psycopg[binary]==3.2.3"

COPY scripts /app/scripts
COPY db /app/db

# Both jobs run as root: the PKI job has to chown the certificate subtrees to the
# accounts the broker and the services run under, which an unprivileged user
# cannot do.
ENTRYPOINT ["/bin/bash", "-c"]
CMD ["bash /app/scripts/provision_fleet.sh"]
