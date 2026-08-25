import { request } from './client'
import type { AccessToken, CurrentUser } from '@/types/api'

export const authApi = {
  login: (email: string, password: string) =>
    request<AccessToken>('/api/v1/auth/login', {
      method: 'POST',
      body: { email, password },
      skipRefresh: true,
    }),

  register: (email: string, password: string, organizationName: string) =>
    request<{ public_id: string; email: string }>('/api/v1/auth/register', {
      method: 'POST',
      body: { email, password, organization_name: organizationName },
      skipRefresh: true,
    }),

  me: () => request<CurrentUser>('/api/v1/auth/me'),

  // No refresh entry point here on purpose. Rotation goes through
  // `refreshSession` in the client, which deduplicates concurrent callers — a
  // second way in would be a way to bypass that, and two refreshes carrying one
  // cookie end in reuse detection revoking the session.

  /** Ask for a reset link. Resolves identically whether or not the address
   *  has an account — the backend will not say, and neither may the UI. */
  forgotPassword: (email: string) =>
    request<{ message: string }>('/api/v1/auth/forgot-password', {
      method: 'POST',
      body: { email },
      skipRefresh: true,
    }),

  /** Redeem a reset link. Returns no session: every existing one was just
   *  revoked, and the user signs in again with the new password. */
  resetPassword: (token: string, newPassword: string) =>
    request<{ message: string }>('/api/v1/auth/reset-password', {
      method: 'POST',
      body: { token, new_password: newPassword },
      skipRefresh: true,
    }),

  /** Revokes the refresh family server-side and clears the refresh cookie.
   *  Best-effort: the client forgets its access token regardless of the
   *  outcome, though only the server can actually end the session. */
  logout: () =>
    request<void>('/api/v1/auth/logout', {
      method: 'POST',
      skipRefresh: true,
    }),
}
