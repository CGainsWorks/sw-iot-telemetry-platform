import type { PlatformStats } from '../lib/types'

interface Props {
  stats: PlatformStats | null
  liveConnected: boolean
}

function formatCount(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k`
  return String(value)
}

export function StatTiles({ stats, liveConnected }: Props) {
  const tiles = [
    { label: 'Devices', value: stats ? String(stats.devices_total) : '-', tone: 'neutral' },
    {
      label: 'Healthy',
      value: stats ? String(stats.devices_healthy) : '-',
      tone: stats && stats.devices_healthy === stats.devices_total ? 'good' : 'neutral',
    },
    {
      label: 'Offline',
      value: stats ? String(stats.devices_offline) : '-',
      tone: stats && stats.devices_offline > 0 ? 'bad' : 'good',
    },
    { label: 'Readings / hour', value: stats ? formatCount(stats.readings_last_hour) : '-', tone: 'neutral' },
    {
      label: 'Active alerts',
      value: stats ? String(stats.active_alerts) : '-',
      tone: stats && stats.active_alerts > 0 ? 'warn' : 'good',
    },
    {
      label: 'Rejected / hour',
      value: stats ? String(stats.dead_letters_last_hour) : '-',
      tone: stats && stats.dead_letters_last_hour > 0 ? 'warn' : 'good',
    },
  ] as const

  return (
    <section className="tiles" aria-label="Platform summary">
      {tiles.map((tile) => (
        <div key={tile.label} className={`tile tile-${tile.tone}`}>
          <span className="tile-value">{tile.value}</span>
          <span className="tile-label">{tile.label}</span>
        </div>
      ))}
      <div className={`tile tile-${liveConnected ? 'good' : 'bad'}`}>
        <span className="tile-value">{liveConnected ? 'live' : 'off'}</span>
        <span className="tile-label">Stream</span>
      </div>
    </section>
  )
}
