/**
 * Types mirroring the API's response models.
 *
 * Hand-written rather than generated from the OpenAPI document. The API is
 * small and stable, and a generator would add a build step and a large amount
 * of machinery for eight endpoints. The trade is that these have to be kept in
 * step by hand; `npm run typecheck` catches the shape, and the integration test
 * catches the rest.
 */

export type Health = 'healthy' | 'stale' | 'degraded' | 'fault' | 'offline' | 'never_seen'

export type Severity = 'info' | 'warning' | 'critical'

export interface DeviceSummary {
  device_id: string
  site_id: string
  profile: string | null
  fw_version: string | null
  schema_version: string | null
  last_seen: string | null
  last_status: string | null
  last_lifecycle_event: string | null
  health: Health
  seconds_since_last_seen: number | null
  certificate_expires_at: string | null
  certificate_days_remaining: number | null
}

export interface DeviceListResponse {
  devices: DeviceSummary[]
  total: number
  by_health: Partial<Record<Health, number>>
}

export interface TelemetryPoint {
  ts: string
  device_id: string
  site_id: string
  metric: string
  value: number
  min_value: number | null
  max_value: number | null
  sample_count: number | null
}

export interface TelemetryResponse {
  points: TelemetryPoint[]
  count: number
  /** Which table answered. Shown in the UI so a surprising chart is explainable. */
  source: string
  resolution: string
  reason: string
  truncated: boolean
}

export interface AlertRecord {
  alert_id: string
  rule_id: string
  rule_name: string | null
  device_id: string
  site_id: string
  metric: string | null
  severity: Severity
  state: 'firing' | 'resolved'
  value: number | null
  threshold: number | null
  message: string
  fired_at: string
  last_seen_at: string
  resolved_at: string | null
  notification_count: number
}

export interface AlertListResponse {
  alerts: AlertRecord[]
  total: number
  by_severity: Partial<Record<Severity, number>>
}

export interface PlatformStats {
  devices_total: number
  devices_healthy: number
  devices_offline: number
  readings_last_hour: number
  readings_last_24h: number
  active_alerts: number
  dead_letters_last_hour: number
  metrics_tracked: number
  oldest_reading: string | null
  newest_reading: string | null
}

export interface MetricInfo {
  name: string
  unit: string
  minimum: number
  maximum: number
  description: string
}

export interface DeadLetterSummary {
  reason: string
  count: number
  latest: string
  example_detail: string
}

export interface DeadLetterResponse {
  summary: DeadLetterSummary[]
  total: number
  window_hours: number
}

export interface SiteRollup {
  site_id: string
  metric: string
  avg_value: number
  min_value: number
  max_value: number
  device_count: number
  sample_count: number
}

/** A frame from the live WebSocket. */
export type LiveFrame =
  | { type: 'hello'; data: { filters: Record<string, string | null>; feed_connected: boolean } }
  | { type: 'reading'; data: LiveReading }
  | { type: 'alert'; data: AlertNotification }

export interface LiveReading {
  ts: string
  device_id: string
  site_id: string
  metric: string
  value: number
  seq: number
  fw_version: string
  schema_version: string
  device_status: string
  ingest_ts: string
}

export interface AlertNotification {
  alert_id: string
  rule_id: string
  rule_name: string
  severity: Severity
  device_id: string
  site_id: string
  metric: string | null
  value: number | null
  threshold: number | null
  message: string
  state: 'firing' | 'resolved'
  fired_at: string
}
