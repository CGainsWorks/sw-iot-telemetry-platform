import type { DeviceSummary, Health } from '../lib/types'

interface Props {
  devices: DeviceSummary[]
  selected: string | null
  onSelect: (deviceId: string) => void
}

const HEALTH_LABEL: Record<Health, string> = {
  healthy: 'Healthy',
  stale: 'Stale',
  degraded: 'Degraded',
  fault: 'Fault',
  offline: 'Offline',
  never_seen: 'Never seen',
}

function age(seconds: number | null): string {
  if (seconds === null) return '-'
  if (seconds < 60) return `${Math.round(seconds)}s`
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`
  return `${Math.round(seconds / 3600)}h`
}

export function DeviceTable({ devices, selected, onSelect }: Props) {
  return (
    <div className="panel">
      <h2>Fleet</h2>
      <div className="table-scroll">
        <table className="device-table">
          <thead>
            <tr>
              <th>Device</th>
              <th>Site</th>
              <th>Health</th>
              <th>Last seen</th>
              <th>Firmware</th>
            </tr>
          </thead>
          <tbody>
            {devices.map((device) => (
              <tr
                key={device.device_id}
                className={device.device_id === selected ? 'selected' : ''}
                onClick={() => onSelect(device.device_id)}
                tabIndex={0}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault()
                    onSelect(device.device_id)
                  }
                }}
              >
                <td className="mono">{device.device_id}</td>
                <td>{device.site_id}</td>
                <td>
                  <span className={`badge badge-${device.health}`}>
                    {HEALTH_LABEL[device.health] ?? device.health}
                  </span>
                </td>
                <td className="mono">{age(device.seconds_since_last_seen)}</td>
                <td className="mono">{device.fw_version ?? '-'}</td>
              </tr>
            ))}
            {devices.length === 0 && (
              <tr>
                <td colSpan={5} className="empty">
                  No devices have reported yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}
