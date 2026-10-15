import type { SiteRollup } from '../lib/types'

interface Props {
  rollups: SiteRollup[]
  metric: string
}

export function SitePanel({ rollups, metric }: Props) {
  const filtered = rollups.filter((row) => row.metric === metric)

  return (
    <div className="panel">
      <h2>Sites</h2>
      {filtered.length === 0 ? (
        <p className="empty">No rollups for {metric} yet.</p>
      ) : (
        <table className="site-table">
          <thead>
            <tr>
              <th>Site</th>
              <th>Devices</th>
              <th>Mean</th>
              <th>Min</th>
              <th>Max</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((row) => (
              <tr key={row.site_id}>
                <td>{row.site_id}</td>
                <td className="mono">{row.device_count}</td>
                <td className="mono">{row.avg_value.toFixed(2)}</td>
                <td className="mono">{row.min_value.toFixed(2)}</td>
                <td className="mono">{row.max_value.toFixed(2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}
