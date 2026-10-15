import type { LiveReading, TelemetryPoint } from './types'

export interface ChartPoint {
  t: number
  value: number
}

/**
 * Merge the historical series with the live tail.
 *
 * The two always overlap: history is fetched at some instant while the socket
 * has already been delivering. Keying by timestamp removes the duplicates and
 * lets the live value win, since that is the more recently observed one.
 * Sorting afterwards means an out-of-order arrival does not draw a line back
 * across the chart.
 *
 * Lives in lib rather than in the chart component so it can be tested without
 * rendering, and so the component file exports only a component.
 */
export function mergeSeries(history: TelemetryPoint[], live: LiveReading[]): ChartPoint[] {
  const byTime = new Map<number, number>()

  for (const point of history) {
    byTime.set(new Date(point.ts).getTime(), point.value)
  }
  for (const point of live) {
    byTime.set(new Date(point.ts).getTime(), point.value)
  }

  return [...byTime.entries()]
    .map(([t, value]) => ({ t, value }))
    .sort((a, b) => a.t - b.t)
}
