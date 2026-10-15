"""The alert state machine.

Every evaluation cycle produces a set of breaches. Turning that set into alerts
is where an alerting system is won or lost, and the three things that matter are:

**Deduplication.** A breach that persists for an hour is one alert, updated,
not two hundred and forty. Enforced by a partial unique index on
``(rule_id, device_id, metric) WHERE state = 'firing'`` rather than by
bookkeeping in this process, so two replicas of the service cannot both open the
same alert.

**Recovery hysteresis.** An alert resolves only after the breach has been absent
for ``recovery``. Without that, a value oscillating around a threshold produces
an endless stream of fire and resolve pairs, and the first thing anyone does
with such a feed is stop reading it.

**Renotification.** A firing alert notifies again at most every ``renotify``,
so a long-running problem stays visible without repeating every cycle.

State lives in the database, not in memory. A restart therefore picks up exactly
where it left off, which is not true of an engine that keeps its state in
process.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

import asyncpg
from prometheus_client import Counter, Gauge

from telemetry_common.logging import get_logger

from .evaluator import Breach, Evaluator, evaluate_all
from .notifiers import DeliveryResult, Notification, Notifier
from .rules import Rule

log = get_logger(__name__)

ALERTS_FIRED = Counter("alerting_fired_total", "Alerts opened", ["rule_id", "severity"])
ALERTS_RESOLVED = Counter("alerting_resolved_total", "Alerts resolved", ["rule_id"])
NOTIFICATIONS = Counter(
    "alerting_notifications_total", "Notification attempts", ["channel", "status"]
)
ACTIVE = Gauge("alerting_active_alerts", "Alerts currently firing")
CYCLE_SECONDS = Gauge("alerting_cycle_seconds", "Duration of the last evaluation cycle")
EVAL_ERRORS = Counter("alerting_evaluation_errors_total", "Failed evaluation cycles")


@dataclass
class EngineConfig:
    #: Seconds between evaluation cycles.
    interval_s: float = 15.0
    #: Rules are re-read from disk this often, so a rule change does not need a
    #: restart. Zero disables reloading.
    reload_interval_s: float = 60.0


@dataclass
class EngineStats:
    cycles: int = 0
    fired: int = 0
    resolved: int = 0
    notifications: int = 0
    errors: int = 0
    started_at: float = field(default_factory=time.monotonic)

    def snapshot(self) -> dict[str, float | int]:
        return {
            "cycles": self.cycles,
            "fired": self.fired,
            "resolved": self.resolved,
            "notifications": self.notifications,
            "errors": self.errors,
            "elapsed_s": round(time.monotonic() - self.started_at, 2),
        }


UPSERT_RULE = """
INSERT INTO alert_rules (rule_id, name, severity, metric, kind, description, definition, enabled)
VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8)
ON CONFLICT (rule_id) DO UPDATE SET
    name        = EXCLUDED.name,
    severity    = EXCLUDED.severity,
    metric      = EXCLUDED.metric,
    kind        = EXCLUDED.kind,
    description = EXCLUDED.description,
    definition  = EXCLUDED.definition,
    enabled     = EXCLUDED.enabled,
    updated_at  = now()
"""

# Opens an alert if none is firing, otherwise refreshes the one that is. The
# partial unique index makes the conflict target exact, so two evaluators racing
# on the same breach produce one row and one of them simply updates it.
FIRE_ALERT = """
INSERT INTO alerts (
    rule_id, device_id, site_id, metric, severity, state,
    value, threshold, message, fired_at, last_seen_at
)
VALUES ($1, $2, $3, $4, $5, 'firing', $6, $7, $8, $9, $9)
ON CONFLICT (rule_id, device_id, COALESCE(metric, '')) WHERE state = 'firing'
DO UPDATE SET
    value        = EXCLUDED.value,
    message      = EXCLUDED.message,
    last_seen_at = EXCLUDED.last_seen_at
RETURNING alert_id::text, fired_at, notification_count,
          (xmax = 0) AS is_new
"""

# xmax = 0 is true only for a genuine insert, so it distinguishes a newly opened
# alert from a refresh of one already firing without a second round trip.

RESOLVE_STALE = """
UPDATE alerts a
SET state = 'resolved', resolved_at = now()
FROM alert_rules r
WHERE a.rule_id = r.rule_id
  AND a.state = 'firing'
  AND now() - a.last_seen_at > make_interval(
        secs => COALESCE((r.definition ->> 'recovery_s')::float8, 120)
      )
RETURNING a.alert_id::text, a.rule_id, a.device_id, a.site_id, a.metric,
          a.severity, a.message, a.fired_at
"""

DUE_FOR_RENOTIFY = """
SELECT a.alert_id::text, a.rule_id, a.device_id, a.site_id, a.metric, a.severity,
       a.value, a.threshold, a.message, a.fired_at, a.notification_count
FROM alerts a
JOIN alert_rules r ON r.rule_id = a.rule_id
WHERE a.state = 'firing'
  AND a.notification_count > 0
  AND now() - a.last_notified_at > make_interval(
        secs => COALESCE((r.definition ->> 'renotify_s')::float8, 3600)
      )
"""


class AlertEngine:
    def __init__(
        self,
        *,
        pool: asyncpg.Pool,
        rules: Sequence[Rule],
        notifiers: dict[str, Notifier],
        config: EngineConfig | None = None,
        reload_rules=None,
    ) -> None:
        self._pool = pool
        self._rules = list(rules)
        self._notifiers = notifiers
        self._config = config or EngineConfig()
        self._reload_rules = reload_rules
        self._evaluator = Evaluator(pool)
        self._stop = asyncio.Event()
        self._last_reload = time.monotonic()
        self.stats = EngineStats()

    def request_stop(self) -> None:
        self._stop.set()

    @property
    def rules(self) -> list[Rule]:
        return list(self._rules)

    # -- rule synchronisation ---------------------------------------------

    async def sync_rules(self) -> None:
        """Write the loaded catalogue into ``alert_rules``.

        Rules that have been deleted from the YAML are disabled rather than
        removed, so the alerts they raised still have a definition to join to.
        """
        import json

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                for rule in self._rules:
                    await conn.execute(
                        UPSERT_RULE,
                        rule.rule_id,
                        rule.name,
                        rule.severity.value,
                        rule.metric,
                        rule.kind.value,
                        rule.description.strip(),
                        json.dumps(rule.definition()),
                        rule.enabled,
                    )
                await conn.execute(
                    "UPDATE alert_rules SET enabled = FALSE, updated_at = now() "
                    "WHERE rule_id <> ALL($1::text[]) AND enabled",
                    [rule.rule_id for rule in self._rules],
                )
        log.info("rules synchronised", count=len(self._rules))

    # -- notification ------------------------------------------------------

    async def _notify(self, notification: Notification, channels: Sequence[str]) -> None:
        results: list[DeliveryResult] = []
        for channel in channels:
            notifier = self._notifiers.get(channel)
            if notifier is None:
                results.append(
                    DeliveryResult(
                        channel=channel, status="failed", detail="no such notifier configured"
                    )
                )
                continue
            results.append(await notifier.send(notification))

        # Alerts always go to the stream as well as to the rule's own channels,
        # so the dashboard sees everything without every rule having to opt in.
        stream = self._notifiers.get("stream")
        if stream is not None and "stream" not in channels:
            results.append(await stream.send(notification))

        async with self._pool.acquire() as conn:
            for result in results:
                NOTIFICATIONS.labels(channel=result.channel, status=result.status).inc()
                await conn.execute(
                    "INSERT INTO notifications (alert_id, channel, status, detail) "
                    "VALUES ($1::uuid, $2, $3, $4)",
                    notification.alert_id,
                    result.channel,
                    result.status,
                    result.detail,
                )
            await conn.execute(
                "UPDATE alerts SET notification_count = notification_count + 1, "
                "last_notified_at = now() WHERE alert_id = $1::uuid",
                notification.alert_id,
            )
        self.stats.notifications += len(results)

    # -- cycle -------------------------------------------------------------

    async def _fire(self, breaches: Sequence[Breach]) -> None:
        now = datetime.now(UTC)
        by_rule = {rule.rule_id: rule for rule in self._rules}

        for breach in breaches:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    FIRE_ALERT,
                    breach.rule.rule_id,
                    breach.device_id,
                    breach.site_id,
                    breach.metric,
                    breach.rule.severity.value,
                    breach.value,
                    breach.rule.threshold,
                    breach.message,
                    now,
                )
            if row is None:
                continue

            if not row["is_new"]:
                # Already firing. Refreshed above; renotification is handled
                # separately so it is not tied to the evaluation interval.
                continue

            ALERTS_FIRED.labels(
                rule_id=breach.rule.rule_id, severity=breach.rule.severity.value
            ).inc()
            self.stats.fired += 1

            rule = by_rule.get(breach.rule.rule_id, breach.rule)
            await self._notify(
                Notification(
                    alert_id=row["alert_id"],
                    rule_id=rule.rule_id,
                    rule_name=rule.name,
                    severity=rule.severity.value,
                    device_id=breach.device_id,
                    site_id=breach.site_id,
                    metric=breach.metric,
                    value=breach.value,
                    threshold=rule.threshold,
                    message=breach.message,
                    state="firing",
                    fired_at=row["fired_at"],
                ),
                rule.channels,
            )

    async def _resolve(self) -> None:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(RESOLVE_STALE)

        by_rule = {rule.rule_id: rule for rule in self._rules}
        for row in rows:
            ALERTS_RESOLVED.labels(rule_id=row["rule_id"]).inc()
            self.stats.resolved += 1
            rule = by_rule.get(row["rule_id"])
            channels = rule.channels if rule else ("log",)
            await self._notify(
                Notification(
                    alert_id=row["alert_id"],
                    rule_id=row["rule_id"],
                    rule_name=rule.name if rule else row["rule_id"],
                    severity=row["severity"],
                    device_id=row["device_id"],
                    site_id=row["site_id"],
                    metric=row["metric"],
                    value=None,
                    threshold=rule.threshold if rule else None,
                    message=f"resolved: {row['message']}",
                    state="resolved",
                    fired_at=row["fired_at"],
                ),
                channels,
            )

    async def _renotify(self) -> None:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(DUE_FOR_RENOTIFY)

        by_rule = {rule.rule_id: rule for rule in self._rules}
        for row in rows:
            rule = by_rule.get(row["rule_id"])
            await self._notify(
                Notification(
                    alert_id=row["alert_id"],
                    rule_id=row["rule_id"],
                    rule_name=rule.name if rule else row["rule_id"],
                    severity=row["severity"],
                    device_id=row["device_id"],
                    site_id=row["site_id"],
                    metric=row["metric"],
                    value=row["value"],
                    threshold=row["threshold"],
                    message=f"still firing: {row['message']}",
                    state="firing",
                    fired_at=row["fired_at"],
                ),
                rule.channels if rule else ("log",),
            )

    async def cycle(self) -> None:
        started = time.perf_counter()
        breaches = await evaluate_all(self._evaluator, self._rules)
        await self._fire(breaches)
        await self._resolve()
        await self._renotify()

        async with self._pool.acquire() as conn:
            active = await conn.fetchval("SELECT count(*) FROM alerts WHERE state = 'firing'")
        ACTIVE.set(active or 0)

        self.stats.cycles += 1
        CYCLE_SECONDS.set(time.perf_counter() - started)

    async def _maybe_reload(self) -> None:
        if self._reload_rules is None or self._config.reload_interval_s <= 0:
            return
        if time.monotonic() - self._last_reload < self._config.reload_interval_s:
            return
        self._last_reload = time.monotonic()
        try:
            rules = self._reload_rules()
        except Exception as exc:
            log.error("rule reload failed, keeping the previous catalogue", error=str(exc))
            return
        if [r.definition() for r in rules] == [r.definition() for r in self._rules]:
            return
        self._rules = list(rules)
        await self.sync_rules()
        log.info("rules reloaded", count=len(self._rules))

    async def run(self) -> EngineStats:
        await self.sync_rules()
        log.info("alert engine started", rules=len(self._rules), interval_s=self._config.interval_s)

        while not self._stop.is_set():
            try:
                await self.cycle()
                await self._maybe_reload()
            except Exception as exc:
                self.stats.errors += 1
                EVAL_ERRORS.inc()
                log.error("evaluation cycle failed", error=str(exc))

            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._config.interval_s)

        log.info("alert engine stopped", **self.stats.snapshot())
        return self.stats


async def run_engine(
    *,
    pool: asyncpg.Pool,
    rules: Sequence[Rule],
    notifiers: dict[str, Notifier],
    config: EngineConfig | None = None,
    reload_rules=None,
) -> EngineStats:
    engine = AlertEngine(
        pool=pool, rules=rules, notifiers=notifiers, config=config, reload_rules=reload_rules
    )
    loop = asyncio.get_running_loop()
    for signame in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, signame, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, engine.request_stop)
        except NotImplementedError:
            signal.signal(sig, lambda *_: engine.request_stop())

    try:
        return await engine.run()
    finally:
        for notifier in notifiers.values():
            await notifier.close()
