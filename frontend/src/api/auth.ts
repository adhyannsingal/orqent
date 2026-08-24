import { request } from './client'
import type { CurrentUser, TokenPair } from '@/types/api'

export const authApi = {
  login: (email: string, password: string) =>
    request<TokenPair>('/api/v1/auth/login', {
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

  /** Exchange a refresh token for a fresh pair. Used on load to restore a
   *  session, since the access token is never persisted. */
  refreshPair: (refreshToken: string) =>
    request<TokenPair>('/api/v1/auth/refresh', {
      method: 'POST',
      body: { refresh_token: refreshToken },
      skipRefresh: true,
    }),

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

  /** Revokes the refresh family server-side. Best-effort: the client clears
   *  its own state regardless of the outcome. */
  logout: (refreshToken: string) =>
    request<void>('/api/v1/auth/logout', {
      method: 'POST',
      body: { refresh_token: refreshToken },
      skipRefresh: true,
    }),
}
