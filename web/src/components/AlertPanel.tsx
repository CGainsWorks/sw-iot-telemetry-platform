import type { AlertRecord } from '../lib/types'

interface Props {
  alerts: AlertRecord[]
  loading: boolean
}

function relative(iso: string): string {
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000
  if (seconds < 60) return `${Math.round(seconds)}s ago`
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`
  return `${Math.round(seconds / 86400)}d ago`
}

export function AlertPanel({ alerts, loading }: Props) {
  return (
    <div className="panel">
      <h2>
        Active alerts <span className="count">{alerts.length}</span>
      </h2>
      {loading && alerts.length === 0 ? (
        <p className="empty">Loading…</p>
      ) : alerts.length === 0 ? (
        <p className="empty">Nothing firing.</p>
      ) : (
        <ul className="alert-list">
          {alerts.map((alert) => (
            <li key={alert.alert_id} className={`alert alert-${alert.severity}`}>
              <div className="alert-head">
                <span className={`badge badge-${alert.severity}`}>{alert.severity}</span>
                <span className="alert-rule">{alert.rule_name ?? alert.rule_id}</span>
                <span className="alert-age">{relative(alert.fired_at)}</span>
              </div>
              <div className="alert-body">
                <span className="mono">{alert.device_id}</span>
                <span className="alert-message">{alert.message}</span>
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
