import type {ApiEnvelope} from './types'

const configuredBase = window.__TRADING_ENGINE_CONFIG__?.apiBaseUrl || '/api/v1'
export const API_BASE_URL = configuredBase.replace(/\/$/, '')

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly details: unknown

  constructor(message: string, status = 0, code = 'REQUEST_FAILED', details: unknown = null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.details = details
  }
}

export function withQuery(path: string, parameters: Record<string, string | number | undefined | null>) {
  const search = new URLSearchParams()
  Object.entries(parameters).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') search.set(key, String(value))
  })
  const suffix = search.toString()
  return suffix ? `${path}${path.includes('?') ? '&' : '?'}${suffix}` : path
}

function normalizeApiPath(path: string): string {
  const queryIndex = path.indexOf('?')

  const pathname =
    queryIndex >= 0
      ? path.slice(0, queryIndex)
      : path

  const query =
    queryIndex >= 0
      ? path.slice(queryIndex)
      : ''

  const normalizedPath = pathname
    .replace(/^\/+/, '')
    .replace(/\/+$/, '')

  return `${normalizedPath}${query}`
}

export async function request<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  let response: Response

  try {
    const csrfToken = document.cookie
      .split(';')
      .map((item) => item.trim())
      .find((item) => item.startsWith('csrftoken='))
      ?.slice('csrftoken='.length)

    const headers = new Headers(options.headers)
    headers.set('Content-Type', 'application/json')

    if (
      options.method &&
      options.method !== 'GET' &&
      csrfToken
    ) {
      headers.set(
        'X-CSRFToken',
        decodeURIComponent(csrfToken),
      )
    }

    const normalizedPath = normalizeApiPath(path)

    response = await fetch(
      `${API_BASE_URL}/${normalizedPath}`,
      {
        ...options,
        credentials: 'include',
        headers,
      },
    )
  } catch (error) {
    const message =
      error instanceof Error
        ? error.message
        : 'Network request failed'

    throw new ApiError(
      `Backend API is unreachable at ${API_BASE_URL}. ${message}`,
      0,
      'NETWORK_ERROR',
    )
  }

  let body: ApiEnvelope<T>

  try {
    body = (await response.json()) as ApiEnvelope<T>
  } catch {
    throw new ApiError(
      `Backend returned an unreadable response (${response.status})`,
      response.status,
    )
  }

  if (!response.ok || !body.ok || body.data === null) {
    throw new ApiError(
      body.error?.message || `Request failed (${response.status})`,
      response.status,
      body.error?.code,
      body.error?.details,
    )
  }

  return body.data
}

export function mutationOptions(method: 'POST' | 'PATCH' | 'DELETE', payload?: unknown, idempotent = false, idempotencyKey?: string): RequestInit {
  const headers: Record<string, string> = {}
  if (idempotent) headers['Idempotency-Key'] = idempotencyKey || crypto.randomUUID()
  return {method, headers, body: payload === undefined ? undefined : JSON.stringify(payload)}
}
