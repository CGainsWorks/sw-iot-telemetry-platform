"""End-to-end tests against the running Compose stack.

    docker compose up -d --wait
    bash scripts/export_certs.sh
    pytest tests/integration -m integration

Skipped automatically when the stack is not up, so ``pytest`` on its own stays
fast and offline.

What these cover that the unit tests cannot: that the broker really does enforce
mutual TLS, that the ACL really does confine a device to its own topic, that a
spoofed payload really is dead-lettered by the running service, and that the
alerting rules fire on the shapes they are meant to and stay quiet on the shapes
they are not.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import ssl
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CERTS = Path(os.environ.get("CERTS_DIR", REPO_ROOT / "certs"))

API_BASE = os.environ.get("API_BASE", "http://localhost:8000")
MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "8883"))
PG_DSN = os.environ.get("PG_DSN", "postgresql://telemetry:telemetry@localhost:5432/telemetry")

# Ingest, then processing, then a rollup flush. Generous because CI machines are
# slower than a laptop and a flaky timing assertion is worse than a slow test.
PIPELINE_TIMEOUT_S = 90.0


def _port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _stack_is_up() -> bool:
    return (
        _port_open("localhost", 8000)
        and _port_open(MQTT_HOST, MQTT_PORT)
        and _port_open("localhost", 5432)
        and (CERTS / "ca" / "ca.crt").exists()
    )


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _stack_is_up(),
        reason="Compose stack is not running, or certs have not been exported. "
        "Run: docker compose up -d --wait && bash scripts/export_certs.sh",
    ),
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


# Function scoped rather than module scoped. A module-scoped async fixture needs
# a module-scoped event loop to match, and the extra pytest-asyncio configuration
# that requires buys nothing here: opening a one-connection pool costs a couple
# of milliseconds.
@pytest.fixture
async def pool():
    import asyncpg

    created = await asyncpg.create_pool(PG_DSN, min_size=1, max_size=4)
    try:
        yield created
    finally:
        await created.close()


@pytest.fixture(scope="module")
def http():
    import httpx

    with httpx.Client(base_url=API_BASE, timeout=20.0) as client:
        yield client


def device_tls(device_id: str):
    import aiomqtt

    directory = CERTS / "devices" / device_id
    return aiomqtt.TLSParameters(
        ca_certs=str(CERTS / "ca" / "ca.crt"),
        certfile=str(directory / f"{device_id}.crt"),
        keyfile=str(directory / f"{device_id}.key"),
        cert_reqs=ssl.CERT_REQUIRED,
    )


async def wait_for(predicate, *, timeout_s: float = PIPELINE_TIMEOUT_S, interval_s: float = 1.0):
    """Poll ``predicate`` until it returns something truthy, or give up.

    Polling rather than a fixed sleep: the pipeline is asynchronous end to end
    and a sleep long enough to be reliable would make the suite crawl.
    """
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        last = await predicate()
        if last:
            return last
        await asyncio.sleep(interval_s)
    return last


# ---------------------------------------------------------------------------
# The pipeline is alive
# ---------------------------------------------------------------------------


class TestPipeline:
    def test_api_is_healthy_and_ready(self, http):
        assert http.get("/healthz").json()["status"] == "ok"
        ready = http.get("/readyz").json()
        assert ready["ready"] is True, ready

    def test_the_fleet_is_reporting(self, http):
        stats = http.get("/api/v1/stats").json()
        assert stats["devices_total"] >= 1, stats
        assert stats["readings_last_hour"] > 0, stats

    def test_every_device_is_registered_with_a_site(self, http):
        devices = http.get("/api/v1/devices").json()["devices"]
        assert devices
        for device in devices:
            assert device["site_id"]
            assert device["fw_version"]
            assert device["health"] in {
                "healthy",
                "stale",
                "degraded",
                "fault",
                "offline",
                "never_seen",
            }

    async def test_raw_readings_reached_timescaledb(self, pool):
        count = await pool.fetchval(
            "SELECT count(*) FROM telemetry WHERE ts > now() - INTERVAL '1 hour'"
        )
        assert count > 0

    async def test_the_stream_processor_maintains_live_rollups(self, pool):
        row = await pool.fetchrow(
            """
            SELECT count(*) AS buckets, max(sample_count) AS biggest
            FROM telemetry_1m_live
            WHERE bucket > now() - INTERVAL '15 minutes'
            """
        )
        assert row["buckets"] > 0
        # A rollup of one sample per bucket would mean the aggregation is not
        # actually aggregating anything.
        assert row["biggest"] > 1

    async def test_rollup_means_match_the_raw_data_they_came_from(self, pool):
        # The one piece of arithmetic nothing downstream can check.
        row = await pool.fetchrow(
            """
            SELECT l.device_id, l.metric, l.bucket, l.avg_value, l.sample_count,
                   r.raw_avg, r.raw_count
            FROM telemetry_1m_live l
            JOIN LATERAL (
                SELECT avg(value) AS raw_avg, count(*) AS raw_count
                FROM telemetry t
                WHERE t.device_id = l.device_id
                  AND t.metric = l.metric
                  AND t.ts >= l.bucket
                  AND t.ts < l.bucket + INTERVAL '1 minute'
            ) r ON TRUE
            WHERE l.bucket BETWEEN now() - INTERVAL '10 minutes' AND now() - INTERVAL '3 minutes'
              AND l.sample_count > 2
            LIMIT 1
            """
        )
        if row is None:
            pytest.skip("no settled rollup bucket available yet")
        assert row["sample_count"] == row["raw_count"]
        assert row["avg_value"] == pytest.approx(float(row["raw_avg"]), rel=1e-9)

    def test_the_query_api_reports_which_source_answered(self, http):
        body = http.get(
            "/api/v1/telemetry", params={"minutes": 30, "metric": "temperature_c"}
        ).json()
        assert body["source"] in {"telemetry", "telemetry_1m_live", "telemetry_1m", "telemetry_1h"}
        assert body["reason"]

    def test_a_window_beyond_raw_retention_is_refused_with_an_explanation(self, http):
        long_ago = datetime.now(UTC) - timedelta(days=120)
        response = http.get(
            "/api/v1/telemetry",
            params={
                "start": long_ago.isoformat(),
                "end": (long_ago + timedelta(hours=1)).isoformat(),
                "resolution": "raw",
            },
        )
        assert response.status_code == 410
        assert "kept for" in response.json()["detail"]


# ---------------------------------------------------------------------------
# The security boundary
# ---------------------------------------------------------------------------


class TestBrokerSecurity:
    async def test_a_client_without_a_certificate_cannot_connect(self):
        """The broker requires mutual TLS, so an anonymous client is refused."""
        import aiomqtt

        anonymous = aiomqtt.TLSParameters(
            ca_certs=str(CERTS / "ca" / "ca.crt"), cert_reqs=ssl.CERT_REQUIRED
        )

        # The refusal can surface in more than one way depending on how far the
        # handshake gets: the broker may reset the connection, fail the TLS
        # handshake outright, or accept the socket and never send a CONNACK.
        # All three are the same result and the test accepts any of them, but
        # the tuple is listed explicitly rather than catching bare Exception, so
        # that a genuine bug in the test itself still fails the test.
        refusals = (aiomqtt.MqttError, ssl.SSLError, OSError, TimeoutError)

        with pytest.raises(refusals):
            async with aiomqtt.Client(
                hostname=MQTT_HOST,
                port=MQTT_PORT,
                identifier="anonymous-intruder",
                tls_params=anonymous,
                timeout=10,
            ):
                pass

    async def test_plaintext_mqtt_is_not_offered_at_all(self):
        # There is deliberately no 1883 listener. A plaintext fallback would
        # make the whole certificate story decorative.
        assert not _port_open(MQTT_HOST, 1883, timeout=1.0)

    async def test_a_device_cannot_publish_to_another_devices_topic(self, pool):
        """The ACL confines each certificate to its own topic space.

        Mosquitto does not reject the publish at the protocol level for QoS 1,
        it silently discards it. What the test asserts is the outcome that
        matters: nothing attributed to the victim appears downstream.
        """
        import aiomqtt

        from telemetry_common.topics import telemetry_topic

        attacker, victim = "esp32-000001", "esp32-000002"
        marker = -77.7  # A plausible rssi value nothing else would produce.

        payload = {
            "schema_version": "1.1.0",
            "device_id": victim,
            "site_id": "site-b",
            "seq": 999_999,
            "ts": datetime.now(UTC).isoformat(),
            "fw_version": "1.4.2",
            "uptime_s": 1,
            "status": "ok",
            "readings": {"rssi_dbm": marker},
        }

        async with aiomqtt.Client(
            hostname=MQTT_HOST,
            port=MQTT_PORT,
            identifier=f"{attacker}-acl-test",
            tls_params=device_tls(attacker),
            timeout=10,
        ) as client:
            await client.publish(
                telemetry_topic(victim, "site-b"), json.dumps(payload).encode(), qos=1
            )

        await asyncio.sleep(10)

        leaked = await pool.fetchval(
            """
            SELECT count(*) FROM telemetry
            WHERE device_id = $1 AND metric = 'rssi_dbm' AND value = $2
              AND ts > now() - INTERVAL '5 minutes'
            """,
            victim,
            marker,
        )
        assert leaked == 0, "a device published telemetry attributed to another device"

    async def test_a_spoofed_payload_on_its_own_topic_is_dead_lettered(self, pool):
        """The core identity check, exercised through the live stack.

        Publishing on your own topic is allowed. Claiming to be someone else in
        the payload is not, because the topic segment is the authenticated
        identity and the payload field is only a claim.
        """
        import aiomqtt

        from telemetry_common.topics import telemetry_topic

        attacker, claimed = "esp32-000003", "esp32-000005"
        before = await pool.fetchval(
            "SELECT count(*) FROM dead_letters WHERE reason = 'identity_mismatch'"
        )

        payload = {
            "schema_version": "1.1.0",
            "device_id": claimed,
            "site_id": "site-c",
            "seq": 424_242,
            "ts": datetime.now(UTC).isoformat(),
            "fw_version": "1.4.2",
            "uptime_s": 1,
            "status": "ok",
            "readings": {"temperature_c": 20.0},
        }

        async with aiomqtt.Client(
            hostname=MQTT_HOST,
            port=MQTT_PORT,
            identifier=f"{attacker}-spoof-test",
            tls_params=device_tls(attacker),
            timeout=10,
        ) as client:
            await client.publish(
                telemetry_topic(attacker, "site-c"), json.dumps(payload).encode(), qos=1
            )

        async def rejected():
            after = await pool.fetchval(
                "SELECT count(*) FROM dead_letters WHERE reason = 'identity_mismatch'"
            )
            return after > before

        assert await wait_for(rejected), "the spoofed payload was not dead-lettered"

        # And nothing was stored under either identity.
        stored = await pool.fetchval(
            "SELECT count(*) FROM telemetry WHERE seq = 424242 AND ts > now() - INTERVAL '5 minutes'"
        )
        assert stored == 0

    async def test_an_unsupported_schema_version_is_dead_lettered(self, pool):
        import aiomqtt

        from telemetry_common.topics import telemetry_topic

        device = "esp32-000006"
        before = await pool.fetchval(
            "SELECT count(*) FROM dead_letters WHERE reason = 'schema_unsupported'"
        )

        payload = {
            "schema_version": "9.0.0",
            "device_id": device,
            "site_id": "site-c",
            "seq": 1,
            "ts": datetime.now(UTC).isoformat(),
            "fw_version": "9.0.0",
            "uptime_s": 1,
            "status": "ok",
            "readings": {"temperature_c": 20.0},
        }

        async with aiomqtt.Client(
            hostname=MQTT_HOST,
            port=MQTT_PORT,
            identifier=f"{device}-schema-test",
            tls_params=device_tls(device),
            timeout=10,
        ) as client:
            await client.publish(
                telemetry_topic(device, "site-c"), json.dumps(payload).encode(), qos=1
            )

        async def rejected():
            after = await pool.fetchval(
                "SELECT count(*) FROM dead_letters WHERE reason = 'schema_unsupported'"
            )
            return after > before

        assert await wait_for(rejected)

    async def test_an_implausible_reading_is_dead_lettered(self, pool):
        import aiomqtt

        from telemetry_common.topics import telemetry_topic

        device = "esp32-000007"
        before = await pool.fetchval(
            "SELECT count(*) FROM dead_letters WHERE reason = 'implausible_reading'"
        )

        payload = {
            "schema_version": "1.1.0",
            "device_id": device,
            "site_id": "site-a",
            "seq": 2,
            "ts": datetime.now(UTC).isoformat(),
            "fw_version": "1.4.2",
            "uptime_s": 1,
            "status": "fault",
            "readings": {"temperature_c": 999.0},
        }

        async with aiomqtt.Client(
            hostname=MQTT_HOST,
            port=MQTT_PORT,
            identifier=f"{device}-range-test",
            tls_params=device_tls(device),
            timeout=10,
        ) as client:
            await client.publish(
                telemetry_topic(device, "site-a"), json.dumps(payload).encode(), qos=1
            )

        async def rejected():
            after = await pool.fetchval(
                "SELECT count(*) FROM dead_letters WHERE reason = 'implausible_reading'"
            )
            return after > before

        assert await wait_for(rejected)


# ---------------------------------------------------------------------------
# Storage policy
# ---------------------------------------------------------------------------


class TestStoragePolicy:
    async def test_the_expected_hypertables_exist(self, pool):
        names = {
            row["hypertable_name"]
            for row in await pool.fetch(
                "SELECT hypertable_name FROM timescaledb_information.hypertables"
            )
        }
        assert {"telemetry", "device_events", "dead_letters", "telemetry_1m_live"} <= names

    async def test_continuous_aggregates_exist_with_the_intended_settings(self, pool):
        rows = {
            row["view_name"]: row
            for row in await pool.fetch(
                "SELECT view_name, materialized_only "
                "FROM timescaledb_information.continuous_aggregates"
            )
        }
        assert {"telemetry_1m", "telemetry_1h"} <= set(rows)
        # telemetry_1h is built on telemetry_1m, and a hierarchical continuous
        # aggregate cannot sit on top of one with real-time aggregation enabled.
        assert rows["telemetry_1m"]["materialized_only"] is True

    async def test_retention_policies_match_what_the_api_believes(self, pool):
        """resolution.py holds a copy of the retention table so it can answer
        "that window is gone" without a round trip. This is the test that stops
        the copy drifting away from the migrations."""
        from api.db import _parse_interval
        from api.resolution import RETENTION

        # ->> rather than -> : asyncpg hands back a jsonb column as a string
        # unless a codec is registered, so extracting the field in SQL avoids
        # the question entirely. Continuous aggregates carry the policy on their
        # internal materialisation hypertable, hence the join back to the
        # catalogue for a name a human recognises.
        rows = await pool.fetch(
            """
            SELECT COALESCE(c.view_name, j.hypertable_name) AS name,
                   j.config ->> 'drop_after'                AS drop_after
            FROM timescaledb_information.jobs j
            LEFT JOIN timescaledb_information.continuous_aggregates c
                   ON c.materialization_hypertable_name = j.hypertable_name
            WHERE j.proc_name = 'policy_retention'
            """
        )
        configured = {row["name"]: _parse_interval(row["drop_after"]) for row in rows}

        for source, expected in RETENTION.items():
            actual = configured.get(source.value)
            assert (
                actual is not None
            ), f"no retention policy for {source.value}. Configured: {sorted(configured)}"
            assert actual == expected, (
                f"{source.value}: the database drops after {actual}, "
                f"api.resolution believes {expected}"
            )

    async def test_compression_is_enabled_on_the_raw_hypertable(self, pool):
        enabled = await pool.fetchval(
            "SELECT compression_enabled FROM timescaledb_information.hypertables "
            "WHERE hypertable_name = 'telemetry'"
        )
        assert enabled is True

    async def test_the_dedupe_index_makes_replay_idempotent(self, pool):
        """At-least-once delivery means the same row arrives twice after a
        restart. Inserting a duplicate must be a no-op, not a duplicate row."""
        row = await pool.fetchrow("SELECT * FROM telemetry ORDER BY ts DESC LIMIT 1")
        if row is None:
            pytest.skip("no telemetry yet")

        inserted = await pool.execute(
            """
            INSERT INTO telemetry (ts, device_id, site_id, metric, value, seq,
                                   fw_version, schema_version, device_status, ingest_ts)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            ON CONFLICT (device_id, metric, ts) DO NOTHING
            """,
            row["ts"],
            row["device_id"],
            row["site_id"],
            row["metric"],
            row["value"],
            row["seq"],
            row["fw_version"],
            row["schema_version"],
            row["device_status"],
            row["ingest_ts"],
        )
        assert inserted == "INSERT 0 0"


# ---------------------------------------------------------------------------
# Alerting
# ---------------------------------------------------------------------------


class TestAlerting:
    async def test_the_rule_catalogue_is_synchronised_into_the_database(self, pool):
        from alerting.rules import load_rules

        catalogue = {rule.rule_id for rule in load_rules(REPO_ROOT / "config" / "alert_rules.yaml")}
        stored = {
            row["rule_id"]
            for row in await pool.fetch("SELECT rule_id FROM alert_rules WHERE enabled")
        }
        assert catalogue <= stored

    async def test_a_sustained_breach_fires_exactly_one_alert(self, pool):
        """Injects a breach directly into the live rollup and waits.

        Going through the simulator would mean waiting for a device to happen to
        get hot, which is neither quick nor deterministic. Writing the rollup the
        rule reads exercises the whole engine: evaluation, the `for` hold,
        deduplication and notification.
        """
        device = "alert-test-device"
        now = datetime.now(UTC).replace(second=0, microsecond=0)

        # high-temperature: avg > 35 for 2 minutes. Three buckets so the hold is
        # comfortably satisfied.
        for minutes_ago in range(3, 0, -1):
            await pool.execute(
                """
                INSERT INTO telemetry_1m_live (
                    bucket, device_id, site_id, metric, avg_value, min_value,
                    max_value, last_value, sample_count, sum_squares, updated_at
                )
                VALUES ($1, $2, 'site-test', 'temperature_c', 48.0, 47.0, 49.0, 48.0, 6, 13824.0, now())
                ON CONFLICT (device_id, metric, bucket) DO UPDATE
                    SET avg_value = EXCLUDED.avg_value
                """,
                now - timedelta(minutes=minutes_ago),
                device,
            )

        try:

            async def fired():
                return await pool.fetchval(
                    "SELECT count(*) FROM alerts "
                    "WHERE device_id = $1 AND rule_id = 'high-temperature' AND state = 'firing'",
                    device,
                )

            assert await wait_for(fired, timeout_s=60), "the threshold rule did not fire"

            # Let several more evaluation cycles run and confirm it stays one row.
            await asyncio.sleep(35)
            count = await pool.fetchval(
                "SELECT count(*) FROM alerts WHERE device_id = $1 AND state = 'firing'", device
            )
            assert count == 1, "the same breach opened more than one alert"

            notified = await pool.fetchval(
                "SELECT notification_count FROM alerts "
                "WHERE device_id = $1 AND state = 'firing'",
                device,
            )
            assert notified >= 1

        finally:
            await pool.execute("DELETE FROM alerts WHERE device_id = $1", device)
            await pool.execute("DELETE FROM telemetry_1m_live WHERE device_id = $1", device)

    async def test_a_duty_cycled_machine_does_not_trip_the_vibration_trend_rule(self, pool):
        """The false positive this rule was originally written with.

        A healthy duty-cycled machine swings between an idle level and a running
        level. A slope taken on the mean fires every time the motor starts. The
        rule tracks the idle floor across the window instead, which duty cycling
        leaves alone.
        """
        device = "duty-cycle-test-device"
        now = datetime.now(UTC).replace(second=0, microsecond=0)

        # 92 minutes, to cover the rule's 90 minute window. Idle for the first
        # 8 minutes of each 20 minute cycle, running for the rest, so each
        # 45 minute half contains two full cycles and therefore an idle period.
        # The floor is identical in both halves, which is the point.
        for minutes_ago in range(92, 0, -1):
            running = (minutes_ago % 20) < 12
            level = 0.32 if running else 0.012
            await pool.execute(
                """
                INSERT INTO telemetry_1m_live (
                    bucket, device_id, site_id, metric, avg_value, min_value,
                    max_value, last_value, sample_count, sum_squares, updated_at
                )
                VALUES ($1, $2, 'site-test', 'vibration_rms_g', $3, $4, $5, $3, 30, 0, now())
                ON CONFLICT (device_id, metric, bucket) DO UPDATE
                    SET avg_value = EXCLUDED.avg_value, min_value = EXCLUDED.min_value
                """,
                now - timedelta(minutes=minutes_ago),
                device,
                level,
                level * 0.9,
                level * 1.1,
            )

        try:
            await asyncio.sleep(35)
            fired = await pool.fetchval(
                "SELECT count(*) FROM alerts "
                "WHERE device_id = $1 AND rule_id = 'vibration-trend' AND state = 'firing'",
                device,
            )
            assert fired == 0, "duty cycling was mistaken for a developing fault"
        finally:
            await pool.execute("DELETE FROM alerts WHERE device_id = $1", device)
            await pool.execute("DELETE FROM telemetry_1m_live WHERE device_id = $1", device)

    async def test_a_rising_idle_floor_does_trip_the_vibration_trend_rule(self, pool):
        """The true positive the rule exists for.

        Same duty cycle, but the idle floor climbs, which is what a developing
        bearing fault looks like.
        """
        device = "bearing-fault-test-device"
        now = datetime.now(UTC).replace(second=0, microsecond=0)

        for minutes_ago in range(92, 0, -1):
            age = 92 - minutes_ago  # 0 oldest, 91 newest
            # Floor doubles roughly every 30 minutes, so it climbs by about 8x
            # across the window: well above the 0.0005 g per minute threshold
            # and nothing like what duty cycling produces.
            floor = 0.012 * (2.0 ** (age / 30.0))
            running = (minutes_ago % 20) < 12
            level = (0.32 + floor) if running else floor
            await pool.execute(
                """
                INSERT INTO telemetry_1m_live (
                    bucket, device_id, site_id, metric, avg_value, min_value,
                    max_value, last_value, sample_count, sum_squares, updated_at
                )
                VALUES ($1, $2, 'site-test', 'vibration_rms_g', $3, $4, $5, $3, 30, 0, now())
                ON CONFLICT (device_id, metric, bucket) DO UPDATE
                    SET avg_value = EXCLUDED.avg_value, min_value = EXCLUDED.min_value
                """,
                now - timedelta(minutes=minutes_ago),
                device,
                level,
                floor,
                level * 1.1,
            )

        try:

            async def fired():
                return await pool.fetchval(
                    "SELECT count(*) FROM alerts "
                    "WHERE device_id = $1 AND rule_id = 'vibration-trend' AND state = 'firing'",
                    device,
                )

            assert await wait_for(
                fired, timeout_s=60
            ), "a rising idle floor did not trip the vibration trend rule"
        finally:
            await pool.execute("DELETE FROM alerts WHERE device_id = $1", device)
            await pool.execute("DELETE FROM telemetry_1m_live WHERE device_id = $1", device)

    async def test_a_half_window_that_never_went_idle_is_not_judged(self, pool):
        """The residual false positive the spread_ratio guard exists for.

        A machine idle through the older half of the window and running
        continuously through the newer half has no idle floor to measure in the
        newer half. Its minimum there sits at the running level, so comparing
        floors reports an enormous rise that is purely the duty cycle.

        Widening the window makes this rare; the guard is what makes it safe.
        """
        device = "no-idle-half-test-device"
        now = datetime.now(UTC).replace(second=0, microsecond=0)

        for minutes_ago in range(92, 0, -1):
            # Older half idle, newer half running flat out and never idle.
            running = minutes_ago <= 46
            level = 0.32 if running else 0.012
            await pool.execute(
                """
                INSERT INTO telemetry_1m_live (
                    bucket, device_id, site_id, metric, avg_value, min_value,
                    max_value, last_value, sample_count, sum_squares, updated_at
                )
                VALUES ($1, $2, 'site-test', 'vibration_rms_g', $3, $4, $5, $3, 30, 0, now())
                ON CONFLICT (device_id, metric, bucket) DO UPDATE
                    SET avg_value = EXCLUDED.avg_value, min_value = EXCLUDED.min_value,
                        max_value = EXCLUDED.max_value
                """,
                now - timedelta(minutes=minutes_ago),
                device,
                level,
                # Tight spread within each bucket: the newer half never dips.
                level * 0.97,
                level * 1.03,
            )

        try:
            await asyncio.sleep(35)
            fired = await pool.fetchval(
                "SELECT count(*) FROM alerts "
                "WHERE device_id = $1 AND rule_id = 'vibration-trend' AND state = 'firing'",
                device,
            )
            assert (
                fired == 0
            ), "a half-window with no idle period was treated as a floor measurement"
        finally:
            await pool.execute("DELETE FROM alerts WHERE device_id = $1", device)
            await pool.execute("DELETE FROM telemetry_1m_live WHERE device_id = $1", device)


# ---------------------------------------------------------------------------
# Live feed
# ---------------------------------------------------------------------------


class TestLiveFeed:
    async def test_the_websocket_delivers_readings(self):
        import websockets

        url = API_BASE.replace("http://", "ws://").replace("https://", "wss://")
        async with websockets.connect(f"{url}/api/v1/stream", open_timeout=15) as socket_:
            hello = json.loads(await asyncio.wait_for(socket_.recv(), timeout=15))
            assert hello["type"] == "hello"
            assert hello["data"]["feed_connected"] is True

            frame = json.loads(await asyncio.wait_for(socket_.recv(), timeout=60))
            assert frame["type"] in {"reading", "alert"}
            if frame["type"] == "reading":
                assert {"device_id", "metric", "value", "ts"} <= set(frame["data"])

    async def test_the_metric_filter_is_applied_server_side(self):
        import websockets

        url = API_BASE.replace("http://", "ws://").replace("https://", "wss://")
        async with websockets.connect(
            f"{url}/api/v1/stream?metric=temperature_c", open_timeout=15
        ) as socket_:
            await asyncio.wait_for(socket_.recv(), timeout=15)  # hello

            seen = 0
            deadline = time.monotonic() + 45
            while seen < 3 and time.monotonic() < deadline:
                frame = json.loads(await asyncio.wait_for(socket_.recv(), timeout=45))
                if frame["type"] == "reading":
                    assert frame["data"]["metric"] == "temperature_c"
                    seen += 1
            assert seen >= 1, "no filtered readings arrived"
