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

COPY services/common /app/services/common
RUN pip install --no-cache-dir ./services/common

COPY edge/simulator /app/edge/simulator
RUN pip install --no-cache-dir ./edge/simulator

USER app

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "telemetry_simulator", "--host", "mosquitto", "--certs-dir", "/certs"]
