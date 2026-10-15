import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, api, streamUrl } from './api'

const originalFetch = globalThis.fetch

afterEach(() => {
  globalThis.fetch = originalFetch
  vi.restoreAllMocks()
})

function mockFetch(response: Partial<Response> & { jsonBody?: unknown }) {
  const fetchMock = vi.fn().mockResolvedValue({
    ok: response.ok ?? true,
    status: response.status ?? 200,
    statusText: response.statusText ?? 'OK',
    json: async () => response.jsonBody,
    text: async () => JSON.stringify(response.jsonBody),
  } as Response)
  globalThis.fetch = fetchMock as unknown as typeof fetch
  return fetchMock
}

describe('api client', () => {
  it('omits undefined and empty query parameters', async () => {
    const fetchMock = mockFetch({ jsonBody: { points: [], count: 0 } })

    await api.telemetry({ device_id: 'esp32-000001', metric: undefined, minutes: 60 })

    const url = new URL(fetchMock.mock.calls[0]![0] as string)
    expect(url.searchParams.get('device_id')).toBe('esp32-000001')
    expect(url.searchParams.get('minutes')).toBe('60')
    expect(url.searchParams.has('metric')).toBe(false)
  })

  it('surfaces the server detail on an error rather than a bare status', async () => {
    mockFetch({
      ok: false,
      status: 410,
      statusText: 'Gone',
      jsonBody: { detail: 'raw samples are kept for 30 days' },
    })

    await expect(api.telemetry({ minutes: 60 })).rejects.toThrow(
      'raw samples are kept for 30 days',
    )
  })

  it('falls back to the status line when the error body is not JSON', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      status: 502,
      statusText: 'Bad Gateway',
      json: async () => {
        throw new Error('not json')
      },
    } as unknown as Response)
    globalThis.fetch = fetchMock as unknown as typeof fetch

    await expect(api.stats()).rejects.toMatchObject({
      name: 'ApiError',
      status: 502,
      message: '502 Bad Gateway',
    })
  })

  it('exposes the HTTP status on the error', async () => {
    mockFetch({ ok: false, status: 404, statusText: 'Not Found', jsonBody: { detail: 'nope' } })

    const error = await api.devices().catch((exc: unknown) => exc)
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).status).toBe(404)
  })
})

describe('streamUrl', () => {
  it('matches the page scheme and carries only the filters that are set', () => {
    const url = new URL(streamUrl({ device_id: 'esp32-000004', metric: 'temperature_c' }))

    expect(url.protocol).toBe('ws:')
    expect(url.pathname).toBe('/api/v1/stream')
    expect(url.searchParams.get('device_id')).toBe('esp32-000004')
    expect(url.searchParams.get('metric')).toBe('temperature_c')
    expect(url.searchParams.has('site_id')).toBe(false)
  })
})
