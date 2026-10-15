# Build context is the repository root, so the shared library can be copied in
# alongside the service.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends tini ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd -g 10001 app \
 && useradd -u 10001 -g 10001 -m -s /usr/sbin/nologin app

WORKDIR /app

# The shared library changes far less often than the service, so it is installed
# first and stays cached across service edits.
COPY services/common /app/services/common
RUN pip install --no-cache-dir ./services/common

COPY services/ingest /app/services/ingest
RUN pip install --no-cache-dir ./services/ingest

USER app
EXPOSE 9100

# tini reaps zombies and, more importantly here, forwards SIGTERM to the Python
# process so the bridge can flush its producer instead of being killed outright.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "ingest.main"]
