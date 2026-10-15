"""FastAPI application factory."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.responses import Response

from telemetry_common.logging import configure_logging, get_logger
from telemetry_common.settings import KafkaSettings, ObservabilitySettings, PostgresSettings

from .db import Database
from .live import LiveFeed
from .routes import router

log = get_logger(__name__)

DESCRIPTION = """
Query API over the IoT telemetry platform.

Telemetry is stored at three resolutions and served from whichever one answers a
query most cheaply. Every telemetry response reports which source it came from
and why, under `source`, `resolution` and `reason`.

Live updates are on the `/api/v1/stream` WebSocket rather than by polling.
"""


def create_app() -> FastAPI:
    obs = ObservabilitySettings(service_name="api")
    configure_logging(service="api", level=obs.log_level, json_output=obs.log_json)

    postgres = PostgresSettings()
    kafka = KafkaSettings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.db = Database(
            postgres.dsn, min_size=postgres.min_pool, max_size=postgres.max_pool
        )
        app.state.live = LiveFeed(
            bootstrap_servers=kafka.bootstrap_servers,
            telemetry_topic=kafka.telemetry_topic,
            alerts_topic=kafka.alerts_topic,
            group_prefix=os.environ.get("API_KAFKA_GROUP_PREFIX", "api-live"),
        )
        await app.state.db.start()
        await app.state.live.start()
        log.info("api ready")
        try:
            yield
        finally:
            # Both are closed even if startup partially failed, so a crash loop
            # does not leak connections into the database or a consumer group.
            await app.state.live.stop()
            await app.state.db.stop()
            log.info("api stopped")

    app = FastAPI(
        title="IoT Telemetry Platform API",
        description=DESCRIPTION,
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )

    origins = [
        origin.strip()
        for origin in os.environ.get("API_CORS_ORIGINS", "http://localhost:5173").split(",")
        if origin.strip()
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    app.include_router(router)

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


app = create_app()
