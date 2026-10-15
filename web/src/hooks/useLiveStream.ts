/**
 * WebSocket subscription to the live telemetry feed.
 *
 * Three things this handles that a bare `new WebSocket(...)` in a component
 * does not:
 *
 * - **Reconnection with backoff.** The API restarts during a deploy; the
 *   dashboard should recover on its own rather than needing a page refresh.
 * - **A bounded buffer.** A live chart holds the last N points and discards the
 *   rest. Without a cap, a tab left open overnight grows until it is killed.
 * - **Strict-mode safety.** React 18 mounts effects twice in development, so the
 *   cleanup has to be genuinely idempotent or every reload leaks a socket.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { streamUrl } from '../lib/api'
import type { AlertNotification, LiveFrame, LiveReading } from '../lib/types'

export type ConnectionState = 'connecting' | 'open' | 'closed'

interface Options {
  device_id?: string
  site_id?: string
  metric?: string
  /** Points retained per series. */
  bufferSize?: number
  enabled?: boolean
}

interface LiveState {
  readings: LiveReading[]
  alerts: AlertNotification[]
  connection: ConnectionState
  received: number
}

const DEFAULT_BUFFER = 600
const MAX_ALERTS = 50
const MAX_BACKOFF_MS = 15_000

export function useLiveStream(options: Options = {}): LiveState {
  const { device_id, site_id, metric, bufferSize = DEFAULT_BUFFER, enabled = true } = options

  const [readings, setReadings] = useState<LiveReading[]>([])
  const [alerts, setAlerts] = useState<AlertNotification[]>([])
  const [connection, setConnection] = useState<ConnectionState>('closed')
  const [received, setReceived] = useState(0)

  const socketRef = useRef<WebSocket | null>(null)
  const retryRef = useRef<number>(0)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const closedByUsRef = useRef(false)

  const handleFrame = useCallback(
    (frame: LiveFrame) => {
      if (frame.type === 'reading') {
        setReadings((current) => {
          const next = [...current, frame.data]
          return next.length > bufferSize ? next.slice(next.length - bufferSize) : next
        })
        setReceived((count) => count + 1)
      } else if (frame.type === 'alert') {
        setAlerts((current) => [frame.data, ...current].slice(0, MAX_ALERTS))
      }
    },
    [bufferSize],
  )

  useEffect(() => {
    if (!enabled) {
      setConnection('closed')
      return
    }

    closedByUsRef.current = false
    // Filters changed, so the buffer describes a different series now.
    setReadings([])

    const connect = () => {
      if (closedByUsRef.current) return

      setConnection('connecting')
      const socket = new WebSocket(streamUrl({ device_id, site_id, metric }))
      socketRef.current = socket

      socket.onopen = () => {
        retryRef.current = 0
        setConnection('open')
      }

      socket.onmessage = (event) => {
        try {
          handleFrame(JSON.parse(event.data as string) as LiveFrame)
        } catch {
          // A malformed frame is not worth tearing the connection down for.
        }
      }

      socket.onclose = () => {
        setConnection('closed')
        if (closedByUsRef.current) return
        // Exponential backoff, capped. Without the cap a long outage would end
        // with the dashboard retrying once an hour and looking broken.
        const delay = Math.min(500 * 2 ** retryRef.current, MAX_BACKOFF_MS)
        retryRef.current += 1
        timerRef.current = setTimeout(connect, delay)
      }

      socket.onerror = () => {
        socket.close()
      }
    }

    connect()

    return () => {
      closedByUsRef.current = true
      if (timerRef.current !== null) {
        clearTimeout(timerRef.current)
        timerRef.current = null
      }
      socketRef.current?.close()
      socketRef.current = null
    }
  }, [device_id, site_id, metric, enabled, handleFrame])

  return { readings, alerts, connection, received }
}

/**
 * Poll an async function on an interval.
 *
 * Used for the panels that read aggregates rather than a live stream: device
 * health, alert history and rejection counts change on the order of seconds, so
 * pushing every change would be more machinery than the problem deserves.
 */
export function usePolling<T>(
  fetcher: () => Promise<T>,
  intervalMs: number,
  deps: unknown[] = [],
): { data: T | null; error: string | null; loading: boolean } {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false

    const run = async () => {
      try {
        const result = await fetcher()
        if (!cancelled) {
          setData(result)
          setError(null)
        }
      } catch (exc) {
        // Keep the last good data on screen. Blanking a dashboard because one
        // poll failed loses the information the operator was looking at.
        if (!cancelled) setError(exc instanceof Error ? exc.message : String(exc))
      } finally {
        if (!cancelled) setLoading(false)
      }
    }

    void run()
    const timer = setInterval(() => void run(), intervalMs)

    return () => {
      cancelled = true
      clearInterval(timer)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, ...deps])

  return { data, error, loading }
}
