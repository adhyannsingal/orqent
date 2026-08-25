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
// The access token lives **in memory only**, and the refresh token is not here
// at all: it travels in an HttpOnly cookie the backend sets, which this code
// cannot read, write, or clear (AH3). That is the whole point — a script that
// compromises this page can still make requests while the tab is open, but it
// cannot exfiltrate a long-lived credential to mint sessions elsewhere at
// leisure. There is no `localStorage` key to find, because there is no value.
//
// The consequence is that ending a session is a *server* action: `logout()`
// asks the backend to revoke the family and delete the cookie. Clearing state
// here alone would end the session in this tab and nowhere else.

let accessToken: string | null = null
let onUnauthenticated: (() => void) | null = null

export function setAccessToken(token: string | null): void {
  accessToken = token
}

/** Forget the in-memory access token. The refresh cookie is the backend's to
 *  remove, and only `/auth/logout` or a rejected `/auth/refresh` does so. */
export function clearAccessToken(): void {
  accessToken = null
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
    // Sends the HttpOnly refresh cookie on the `/auth` calls that need it.
    // Applied to every request rather than just those three because
    // `API_BASE_URL` is a single configured origin — this app's own backend —
    // so there is no third party for a credential to leak to. Making it
    // conditional would be one more thing to get wrong on a new endpoint.
    credentials: 'include',
  })
}

/**
 * Ask the backend to rotate the refresh cookie and issue a new access token.
 *
 * Concurrent 401s share one in-flight refresh, and this matters more after AH3
 * rather than less: a page rendering four queries would otherwise fire four
 * refreshes carrying the *same* cookie, and the backend's rotation-with-reuse
 * detection would correctly treat the losers as stolen tokens and revoke the
 * whole family — logging the user out for the crime of loading a page.
 */
let refreshInFlight: Promise<boolean> | null = null

export async function refreshSession(): Promise<boolean> {
  // No precondition to check any more. Before AH3 this returned early when
  // `localStorage` held no token; now the cookie is invisible here, so the only
  // way to learn whether a session exists is to ask. A missing cookie is a 401,
  // which is the same "no" one step later.
  refreshInFlight ??= (async () => {
    try {
      const response = await send('/api/v1/auth/refresh', {
        method: 'POST',
        skipRefresh: true,
      })
      if (!response.ok) return false
      // The rotated refresh token arrived as a `Set-Cookie` the browser has
      // already stored; only the access token is in this body.
      const issued = (await response.json()) as { access_token: string }
      setAccessToken(issued.access_token)
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
      clearAccessToken()
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
