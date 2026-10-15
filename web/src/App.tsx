import { useEffect, useMemo, useState } from 'react'
import { AlertPanel } from './components/AlertPanel'
import { DeadLetterPanel } from './components/DeadLetterPanel'
import { DeviceTable } from './components/DeviceTable'
import { LiveChart } from './components/LiveChart'
import { SitePanel } from './components/SitePanel'
import { StatTiles } from './components/StatTiles'
import { usePolling, useLiveStream } from './hooks/useLiveStream'
import { api } from './lib/api'
import type { MetricInfo, TelemetryResponse } from './lib/types'

/**
 * Polling intervals.
 *
 * The chart does not appear here: it is fed by the WebSocket, and re-fetching
 * history on a timer would fight the live tail for the same pixels. History is
 * fetched once per device or metric change and the socket takes it from there.
 */
const STATS_INTERVAL = 5_000
const DEVICES_INTERVAL = 5_000
const ALERTS_INTERVAL = 5_000
const DLQ_INTERVAL = 15_000
const SITES_INTERVAL = 15_000

const HISTORY_MINUTES = 60

export default function App() {
  const [selectedDevice, setSelectedDevice] = useState<string | null>(null)
  const [metric, setMetric] = useState<string>('temperature_c')
  const [metrics, setMetrics] = useState<MetricInfo[]>([])
  const [history, setHistory] = useState<TelemetryResponse | null>(null)
  const [historyLoading, setHistoryLoading] = useState(true)
  const [historyError, setHistoryError] = useState<string | null>(null)

  const stats = usePolling(() => api.stats(), STATS_INTERVAL)
  const devices = usePolling(() => api.devices(), DEVICES_INTERVAL)
  const alerts = usePolling(() => api.alerts({ state: 'firing', limit: 50 }), ALERTS_INTERVAL)
  const deadLetters = usePolling(() => api.deadLetters(24), DLQ_INTERVAL)
  const sites = usePolling(() => api.siteRollup(1), SITES_INTERVAL)

  const live = useLiveStream({
    device_id: selectedDevice ?? undefined,
    metric,
    enabled: Boolean(selectedDevice),
  })

  // The metric catalogue is fixed for the life of the page.
  useEffect(() => {
    void api
      .metrics()
      .then(setMetrics)
      .catch(() => setMetrics([]))
  }, [])

  // Pick the first device once the fleet list arrives, so the chart is not
  // empty on first paint.
  useEffect(() => {
    if (selectedDevice === null && devices.data && devices.data.devices.length > 0) {
      setSelectedDevice(devices.data.devices[0]!.device_id)
    }
  }, [devices.data, selectedDevice])

  // Fetch history whenever the selection changes. The live socket covers
  // everything after this point.
  useEffect(() => {
    if (!selectedDevice) return
    let cancelled = false
    setHistoryLoading(true)

    void api
      .telemetry({ device_id: selectedDevice, metric, minutes: HISTORY_MINUTES })
      .then((response) => {
        if (!cancelled) {
          setHistory(response)
          setHistoryError(null)
        }
      })
      .catch((exc: unknown) => {
        if (!cancelled) setHistoryError(exc instanceof Error ? exc.message : String(exc))
      })
      .finally(() => {
        if (!cancelled) setHistoryLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [selectedDevice, metric])

  const metricInfo = useMemo(
    () => metrics.find((item) => item.name === metric),
    [metrics, metric],
  )

  const availableMetrics = metrics.length > 0 ? metrics.map((m) => m.name) : [metric]

  return (
    <div className="app">
      <header className="app-header">
        <div>
          <h1>IoT Telemetry Platform</h1>
          <p className="subtitle">
            Simulated fleet, mutual-TLS ingest, stream rollups, TimescaleDB and live alerting.
          </p>
        </div>
        <label className="metric-picker">
          <span>Metric</span>
          <select value={metric} onChange={(event) => setMetric(event.target.value)}>
            {availableMetrics.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </label>
      </header>

      <StatTiles stats={stats.data} liveConnected={live.connection === 'open'} />

      {stats.error && (
        <p className="banner banner-error">API unreachable: {stats.error}</p>
      )}

      <main className="grid">
        <div className="column column-wide">
          {historyError ? (
            <div className="panel">
              <h2>{metric}</h2>
              <p className="banner banner-error">{historyError}</p>
            </div>
          ) : (
            <LiveChart
              history={history?.points ?? []}
              live={live.readings}
              metric={metric}
              metricInfo={metricInfo}
              source={history?.source ?? null}
              reason={history?.reason ?? null}
              loading={historyLoading}
            />
          )}
          <DeviceTable
            devices={devices.data?.devices ?? []}
            selected={selectedDevice}
            onSelect={setSelectedDevice}
          />
        </div>

        <div className="column">
          <AlertPanel alerts={alerts.data?.alerts ?? []} loading={alerts.loading} />
          <SitePanel rollups={sites.data ?? []} metric={metric} />
          <DeadLetterPanel data={deadLetters.data} />
        </div>
      </main>

      <footer className="app-footer">
        <span>
          {selectedDevice ? (
            <>
              Watching <code>{selectedDevice}</code> · {live.received} live samples
            </>
          ) : (
            'Select a device to watch it live'
          )}
        </span>
        <span className={`conn conn-${live.connection}`}>stream: {live.connection}</span>
      </footer>
    </div>
  )
}
