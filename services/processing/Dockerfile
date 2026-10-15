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

COPY services/processing /app/services/processing
RUN pip install --no-cache-dir ./services/processing

USER app
EXPOSE 9100

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "processing.main"]
