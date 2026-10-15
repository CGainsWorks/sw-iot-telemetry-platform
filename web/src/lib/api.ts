/**
 * Typed client for the query API.
 *
 * Everything goes through one `request` helper so a failure surfaces the
 * server's `detail` field rather than a bare "Failed to fetch". The API returns
 * 410 with an explanation when a window falls outside retention, and that
 * message is worth showing to the user verbatim.
 */

import type {
  AlertListResponse,
  DeadLetterResponse,
  DeviceListResponse,
  MetricInfo,
  PlatformStats,
  SiteRollup,
  TelemetryResponse,
} from './types'

/**
 * Empty by default: nginx and the Vite dev server both proxy /api to the API
 * container, so the browser talks to its own origin and no CORS preflight
 * happens. Overridable for a deployment where the API is on another host.
 */
const BASE = import.meta.env.VITE_API_BASE ?? ''

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

async function request<T>(path: string, params?: Record<string, string | number | undefined>): Promise<T> {
  const url = new URL(`${BASE}${path}`, window.location.origin)
  for (const [key, value] of Object.entries(params ?? {})) {
    if (value !== undefined && value !== '') {
      url.searchParams.set(key, String(value))
    }
  }

  const response = await fetch(url.toString(), { headers: { accept: 'application/json' } })

  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`
    try {
      const body = (await response.json()) as { detail?: string }
      if (body.detail) detail = body.detail
    } catch {
      // A non-JSON error body is not worth failing over; the status line stands.
    }
    throw new ApiError(detail, response.status)
  }

  return (await response.json()) as T
}

export const api = {
  stats: () => request<PlatformStats>('/api/v1/stats'),

  metrics: () => request<MetricInfo[]>('/api/v1/metrics'),

  devices: (params?: { site_id?: string; health?: string }) =>
    request<DeviceListResponse>('/api/v1/devices', params),

  telemetry: (params: {
    minutes?: number
    device_id?: string
    site_id?: string
    metric?: string
    resolution?: string
    limit?: number
  }) => request<TelemetryResponse>('/api/v1/telemetry', params),

  alerts: (params?: { state?: string; severity?: string; device_id?: string; limit?: number }) =>
    request<AlertListResponse>('/api/v1/alerts', params),

  deadLetters: (hours = 24) => request<DeadLetterResponse>('/api/v1/dead-letters', { hours }),

  siteRollup: (hours = 1) => request<SiteRollup[]>('/api/v1/sites/rollup', { hours }),
}

/** WebSocket URL for the live feed, matching the page's scheme and host. */
export function streamUrl(filters: { device_id?: string; site_id?: string; metric?: string }): string {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const url = new URL(`${protocol}//${window.location.host}${BASE}/api/v1/stream`)
  for (const [key, value] of Object.entries(filters)) {
    if (value) url.searchParams.set(key, value)
  }
  return url.toString()
}
