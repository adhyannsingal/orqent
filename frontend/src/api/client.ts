/**
 * The single place the browser talks to Orqent.
 *
 * Every request in the app goes through `request()`. That centralisation is
 * what makes three security properties enforceable rather than aspirational:
 * the access token is attached in exactly one place, a `401` clears the session
 * in exactly one place, and a backend error becomes a typed `ApiError` instead
 * of a raw exception body reaching a component.
 *
 * **The browser never calls a provider.** There is no Gemini client here and
 * there must never be one: the browser talks to Orqent, Orqent's worker talks
 * to Gemini. A provider key in this bundle would be a public credential.
 */

import type { ErrorResponse } from '@/types/api'

/** Configurable, and the only environment variable this app has. */
export const API_BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? ''

/** A backend failure, carrying the error envelope for callers that can use it. */
export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly details: { code: string; message: string; field?: string | null }[]

  constructor(status: number, body: ErrorResponse | null, fallback: string) {
    super(body?.error?.message || fallback)
    this.name = 'ApiError'
    this.status = status
    this.code = body?.error?.code ?? 'unknown_error'
    this.details = body?.error?.details ?? []
  }
}

// --- Token handling ----------------------------------------------------------
//
// The access token lives **in memory only**. A refresh token is persisted,
// because without it every page reload would log the user out — but the
// short-lived credential that actually authorises requests is never written to
// storage, so a successful XSS cannot read it from `localStorage` at leisure.
// This is the safest split the backend's bearer-token model allows; it does not
// support cookie sessions, and inventing one would mean changing the backend.

let accessToken: string | null = null
let onUnauthenticated: (() => void) | null = null

const REFRESH_KEY = 'orqent.refresh'

export function setAccessToken(token: string | null): void {
  accessToken = token
}

export function getRefreshToken(): string | null {
  try {
    return localStorage.getItem(REFRESH_KEY)
  } catch {
    return null
  }
}

export function setRefreshToken(token: string | null): void {
  try {
    if (token) localStorage.setItem(REFRESH_KEY, token)
    else localStorage.removeItem(REFRESH_KEY)
  } catch {
    /* storage unavailable (private mode); the session simply won't survive reload */
  }
}

export function clearTokens(): void {
  accessToken = null
  setRefreshToken(null)
}

/** Registered once by the auth store, so a 401 can end the session globally. */
export function setUnauthenticatedHandler(handler: (() => void) | null): void {
  onUnauthenticated = handler
}

// --- The request pipeline ----------------------------------------------------

interface RequestOptions {
  method?: string
  body?: unknown
  /** Set for the refresh call itself, so a failed refresh cannot recurse. */
  skipRefresh?: boolean
  signal?: AbortSignal
}

async function parseError(response: Response, fallback: string): Promise<ApiError> {
  let body: ErrorResponse | null = null
  try {
    body = (await response.json()) as ErrorResponse
  } catch {
    /* a non-JSON failure (proxy, gateway) keeps the generic message */
  }
  return new ApiError(response.status, body, fallback)
}

async function send(path: string, options: RequestOptions): Promise<Response> {
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (options.body !== undefined) headers['Content-Type'] = 'application/json'
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`

  return fetch(`${API_BASE_URL}${path}`, {
    method: options.method ?? 'GET',
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    signal: options.signal,
  })
}

/**
 * Trade the stored refresh token for a new pair.
 *
 * Concurrent 401s share one in-flight refresh: without that, a page rendering
 * four queries would fire four refreshes, and the backend's rotation-with-reuse
 * detection would treat the losers as stolen tokens and revoke the family.
 */
let refreshInFlight: Promise<boolean> | null = null

async function refreshSession(): Promise<boolean> {
  const refresh = getRefreshToken()
  if (!refresh) return false

  refreshInFlight ??= (async () => {
    try {
      const response = await send('/api/v1/auth/refresh', {
        method: 'POST',
        body: { refresh_token: refresh },
        skipRefresh: true,
      })
      if (!response.ok) return false
      const pair = (await response.json()) as { access_token: string; refresh_token: string }
      setAccessToken(pair.access_token)
      setRefreshToken(pair.refresh_token)
      return true
    } catch {
      return false
    } finally {
      refreshInFlight = null
    }
  })()

  return refreshInFlight
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  let response = await send(path, options)

  if (response.status === 401 && !options.skipRefresh) {
    // One attempt to renew, then the session is genuinely over.
    if (await refreshSession()) {
      response = await send(path, options)
    }
    if (response.status === 401) {
      clearTokens()
      onUnauthenticated?.()
      throw await parseError(response, 'Your session has expired. Please sign in again.')
    }
  }

  if (!response.ok) {
    throw await parseError(response, `Request failed (${response.status}).`)
  }

  if (response.status === 204 || response.headers.get('content-length') === '0') {
    return undefined as T
  }
  return (await response.json()) as T
}

/** A user-safe message for any thrown value. Never a stack trace. */
export function messageOf(error: unknown, fallback = 'Something went wrong.'): string {
  if (error instanceof ApiError) return error.message
  if (error instanceof Error && error.message) return error.message
  return fallback
}
