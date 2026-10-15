import type { DeadLetterResponse } from '../lib/types'

interface Props {
  data: DeadLetterResponse | null
}

/**
 * Human-readable names for the machine-readable rejection reasons ingest emits.
 * Kept here rather than sent by the API so the wire value stays stable for
 * anything else consuming it.
 */
const REASON_LABEL: Record<string, string> = {
  malformed_topic: 'Malformed topic',
  malformed_json: 'Malformed JSON',
  schema_unsupported: 'Unsupported schema version',
  schema_invalid: 'Failed schema validation',
  identity_mismatch: 'Payload identity did not match the certificate',
  site_mismatch: 'Payload site did not match the topic',
  implausible_reading: 'Reading outside the catalogue range',
  timestamp_out_of_range: 'Timestamp too far from the platform clock',
}

export function DeadLetterPanel({ data }: Props) {
  const summary = data?.summary ?? []

  return (
    <div className="panel">
      <h2>
        Rejected messages{' '}
        <span className="count">{data ? `${data.total} / ${data.window_hours}h` : '-'}</span>
      </h2>
      {summary.length === 0 ? (
        <p className="empty">Nothing rejected in this window.</p>
      ) : (
        <ul className="dlq-list">
          {summary.map((item) => (
            <li key={item.reason}>
              <div className="dlq-head">
                <span className="dlq-reason">{REASON_LABEL[item.reason] ?? item.reason}</span>
                <span className="count">{item.count}</span>
              </div>
              <p className="dlq-detail mono">{item.example_detail}</p>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
