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

COPY services/api /app/services/api
RUN pip install --no-cache-dir ./services/api

USER app
EXPOSE 8000

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "api.main", "--host", "0.0.0.0", "--port", "8000"]
