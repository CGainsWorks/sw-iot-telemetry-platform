import { useMemo } from 'react'
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { mergeSeries } from '../lib/series'
import type { LiveReading, MetricInfo, TelemetryPoint } from '../lib/types'

interface Props {
  /** Historical points from the query API. */
  history: TelemetryPoint[]
  /** Points that have arrived on the WebSocket since the page loaded. */
  live: LiveReading[]
  metric: string
  metricInfo: MetricInfo | undefined
  source: string | null
  reason: string | null
  loading: boolean
}

function formatTime(value: number): string {
  return new Date(value).toLocaleTimeString('en-GB', { hour12: false })
}

export function LiveChart({ history, live, metric, metricInfo, source, reason, loading }: Props) {
  const data = useMemo(() => mergeSeries(history, live), [history, live])

  const unit = metricInfo?.unit ?? ''
  const title = metricInfo ? `${metric} (${unit})` : metric

  return (
    <div className="panel panel-chart">
      <div className="panel-header">
        <h2>{title}</h2>
        {source && (
          <span className="source-note" title={reason ?? undefined}>
            served from <code>{source}</code>
          </span>
        )}
      </div>

      {loading && data.length === 0 ? (
        <p className="empty">Loading…</p>
      ) : data.length === 0 ? (
        <p className="empty">No samples in this window.</p>
      ) : (
        <ResponsiveContainer width="100%" height={280}>
          <LineChart data={data} margin={{ top: 8, right: 16, bottom: 8, left: 0 }}>
            <CartesianGrid stroke="var(--grid)" strokeDasharray="3 3" />
            <XAxis
              dataKey="t"
              type="number"
              domain={['dataMin', 'dataMax']}
              tickFormatter={formatTime}
              stroke="var(--muted)"
              fontSize={12}
            />
            <YAxis
              stroke="var(--muted)"
              fontSize={12}
              width={64}
              domain={['auto', 'auto']}
              tickFormatter={(value: number) => value.toFixed(2)}
            />
            <Tooltip
              contentStyle={{
                background: 'var(--surface)',
                border: '1px solid var(--border)',
                borderRadius: 6,
                color: 'var(--text)',
              }}
              labelFormatter={(value) => formatTime(Number(value))}
              formatter={(value: number) => [`${value.toFixed(3)} ${unit}`, metric]}
            />
            <Line
              type="monotone"
              dataKey="value"
              stroke="var(--accent)"
              strokeWidth={1.8}
              // Thousands of dots would dominate the line and cost more to
              // render than the line itself.
              dot={false}
              isAnimationActive={false}
            />
          </LineChart>
        </ResponsiveContainer>
      )}

      <p className="chart-footnote">
        {data.length} points{live.length > 0 && `, ${live.length} live`}
      </p>
    </div>
  )
}
