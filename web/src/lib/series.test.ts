import { describe, expect, it } from 'vitest'
import { mergeSeries } from './series'
import type { LiveReading, TelemetryPoint } from './types'

function historical(ts: string, value: number): TelemetryPoint {
  return {
    ts,
    device_id: 'esp32-000001',
    site_id: 'site-a',
    metric: 'temperature_c',
    value,
    min_value: null,
    max_value: null,
    sample_count: null,
  }
}

function liveReading(ts: string, value: number): LiveReading {
  return {
    ts,
    device_id: 'esp32-000001',
    site_id: 'site-a',
    metric: 'temperature_c',
    value,
    seq: 1,
    fw_version: '1.4.2',
    schema_version: '1.1.0',
    device_status: 'ok',
    ingest_ts: ts,
  }
}

describe('mergeSeries', () => {
  it('returns points in time order regardless of arrival order', () => {
    const merged = mergeSeries(
      [historical('2026-08-22T10:00:00Z', 1)],
      [liveReading('2026-08-22T10:00:30Z', 3), liveReading('2026-08-22T10:00:10Z', 2)],
    )

    expect(merged.map((p) => p.value)).toEqual([1, 2, 3])
  })

  it('lets the live value win where history and the socket overlap', () => {
    // History is fetched at some instant while the socket is already
    // delivering, so the two always overlap at the join. The live value is the
    // one that was actually most recently observed.
    const merged = mergeSeries(
      [historical('2026-08-22T10:00:00Z', 20.0)],
      [liveReading('2026-08-22T10:00:00Z', 20.5)],
    )

    expect(merged).toHaveLength(1)
    expect(merged[0]!.value).toBe(20.5)
  })

  it('handles an empty live tail', () => {
    const merged = mergeSeries([historical('2026-08-22T10:00:00Z', 1)], [])
    expect(merged).toHaveLength(1)
  })

  it('handles no history at all', () => {
    const merged = mergeSeries([], [liveReading('2026-08-22T10:00:00Z', 4)])
    expect(merged).toEqual([{ t: Date.parse('2026-08-22T10:00:00Z'), value: 4 }])
  })
})
