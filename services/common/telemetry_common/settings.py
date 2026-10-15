"""Environment-driven settings shared by every service.

One class per external dependency, each with a prefix, so a service composes
only the pieces it actually talks to. Defaults point at the Docker Compose
stack; production overrides come from the environment.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class MqttSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MQTT_", extra="ignore")

    host: str = "mosquitto"
    port: int = 8883
    tls: bool = True
    ca_cert: str = "/certs/ca.crt"
    client_cert: str = "/certs/client.crt"
    client_key: str = "/certs/client.key"
    keepalive_s: int = 30
    qos: int = 1
    tenant: str = "acme"
    #: TLS server-name check. Off for the local broker whose certificate is
    #: issued to "mosquitto" but reached under several names inside Compose.
    verify_hostname: bool = False


class KafkaSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KAFKA_", extra="ignore")

    bootstrap_servers: str = "redpanda:9092"
    telemetry_topic: str = "telemetry.normalised"
    status_topic: str = "device.status"
    dlq_topic: str = "telemetry.deadletter"
    alerts_topic: str = "alerts.fired"
    consumer_group: str = "telemetry-platform"
    #: Batch linger. Higher trades latency for throughput; measured in bench/.
    linger_ms: int = 20
    compression: str = "lz4"


class PostgresSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PG_", extra="ignore")

    host: str = "timescaledb"
    port: int = 5432
    database: str = "telemetry"
    user: str = "telemetry"
    password: str = "telemetry"
    min_pool: int = 2
    max_pool: int = 10

    @property
    def dsn(self) -> str:
        return (
            f"postgresql://{self.user}:{self.password}" f"@{self.host}:{self.port}/{self.database}"
        )


class ObservabilitySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OBS_", extra="ignore")

    log_level: str = "INFO"
    log_json: bool = True
    service_name: str = Field(default="telemetry-service")
    metrics_port: int = 9100
