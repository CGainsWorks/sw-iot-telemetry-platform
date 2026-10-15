-- 0005: alerting state.
--
-- Rules live in config/alert_rules.yaml and are synchronised into alert_rules on
-- startup. The YAML is the source of truth so rules are reviewable in a pull
-- request; the table exists so the query API can join alerts to their definition
-- without parsing YAML, and so a rule that has been deleted still explains the
-- alerts it produced.

CREATE TABLE IF NOT EXISTS alert_rules (
    rule_id     TEXT PRIMARY KEY,
    name        TEXT        NOT NULL,
    severity    TEXT        NOT NULL,
    metric      TEXT,
    kind        TEXT        NOT NULL,
    description TEXT        NOT NULL DEFAULT '',
    -- Full rule definition as loaded from YAML, so the engine and the API always
    -- agree about what a rule means without a second parser.
    definition  JSONB       NOT NULL,
    enabled     BOOLEAN     NOT NULL DEFAULT TRUE,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS alerts (
    alert_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_id            TEXT        NOT NULL REFERENCES alert_rules (rule_id) ON DELETE CASCADE,
    device_id          TEXT        NOT NULL,
    site_id            TEXT        NOT NULL,
    metric             TEXT,
    severity           TEXT        NOT NULL,
    state              TEXT        NOT NULL CHECK (state IN ('firing', 'resolved')),
    value              DOUBLE PRECISION,
    threshold          DOUBLE PRECISION,
    message            TEXT        NOT NULL,
    fired_at           TIMESTAMPTZ NOT NULL,
    -- Refreshed every cycle the breach is still present. Resolution is decided by
    -- how far this has fallen behind now(), which is what gives recovery its
    -- hysteresis without any state in the alerting process.
    last_seen_at       TIMESTAMPTZ NOT NULL,
    resolved_at        TIMESTAMPTZ,
    notification_count INTEGER     NOT NULL DEFAULT 0,
    -- Separate from last_seen_at: an alert is refreshed every cycle but
    -- renotified at most once per the rule's renotify interval.
    last_notified_at   TIMESTAMPTZ
);

-- One live alert per rule per device per metric. This partial unique index is
-- the deduplication: a breach that persists for an hour updates one row rather
-- than producing an alert per evaluation, which is the difference between a
-- useful alert feed and a pager that gets muted on day one.
CREATE UNIQUE INDEX IF NOT EXISTS alerts_active_uniq_idx
    ON alerts (rule_id, device_id, COALESCE(metric, ''))
    WHERE state = 'firing';

CREATE INDEX IF NOT EXISTS alerts_state_fired_idx ON alerts (state, fired_at DESC);
CREATE INDEX IF NOT EXISTS alerts_device_idx      ON alerts (device_id, fired_at DESC);
CREATE INDEX IF NOT EXISTS alerts_site_idx        ON alerts (site_id, fired_at DESC);

CREATE TABLE IF NOT EXISTS notifications (
    id       BIGSERIAL PRIMARY KEY,
    alert_id UUID        NOT NULL REFERENCES alerts (alert_id) ON DELETE CASCADE,
    channel  TEXT        NOT NULL,
    status   TEXT        NOT NULL CHECK (status IN ('sent', 'failed', 'suppressed')),
    detail   TEXT,
    sent_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS notifications_alert_idx ON notifications (alert_id, sent_at DESC);

-- ---------------------------------------------------------------------------
-- Currently firing alerts, joined to their rule. What the dashboard shows.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW active_alerts AS
SELECT
    a.alert_id,
    a.rule_id,
    r.name AS rule_name,
    r.kind AS rule_kind,
    a.device_id,
    a.site_id,
    a.metric,
    a.severity,
    a.value,
    a.threshold,
    a.message,
    a.fired_at,
    a.last_seen_at,
    a.notification_count,
    EXTRACT(EPOCH FROM (now() - a.fired_at)) AS age_seconds
FROM alerts a
JOIN alert_rules r ON r.rule_id = a.rule_id
WHERE a.state = 'firing'
ORDER BY
    CASE a.severity
        WHEN 'critical' THEN 0
        WHEN 'warning'  THEN 1
        ELSE 2
    END,
    a.fired_at DESC;
