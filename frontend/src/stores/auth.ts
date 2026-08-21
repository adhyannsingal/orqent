import { create } from 'zustand'
import { authApi } from '@/api/auth'
import {
  clearTokens, getRefreshToken, setAccessToken, setRefreshToken, setUnauthenticatedHandler,
} from '@/api/client'
import type { CurrentUser } from '@/types/api'

interface AuthState {
  user: CurrentUser | null
  /** True until the initial "am I still signed in?" check settles, so the app
   *  doesn't flash the login screen at someone who is already authenticated. */
  initializing: boolean
  login: (email: string, password: string) => Promise<void>
  register: (email: string, password: string, organizationName: string) => Promise<void>
  logout: () => Promise<void>
  restore: () => Promise<void>
}

export const useAuth = create<AuthState>((set) => ({
  user: null,
  initializing: true,

  login: async (email, password) => {
    const pair = await authApi.login(email, password)
    setAccessToken(pair.access_token)
    setRefreshToken(pair.refresh_token)
    set({ user: await authApi.me() })
  },

  register: async (email, password, organizationName) => {
    await authApi.register(email, password, organizationName)
    const pair = await authApi.login(email, password)
    setAccessToken(pair.access_token)
    setRefreshToken(pair.refresh_token)
    set({ user: await authApi.me() })
  },

  logout: async () => {
    const refresh = getRefreshToken()
    // Revoking server-side is best effort; the local session ends either way.
    if (refresh) await authApi.logout(refresh).catch(() => undefined)
    clearTokens()
    set({ user: null })
  },

  /**
   * Re-establish a session on load from the persisted refresh token.
   *
   * The access token is deliberately not persisted, so this exchange is what
   * makes a reload survivable at all.
   */
  restore: async () => {
    const refresh = getRefreshToken()
    if (!refresh) return set({ initializing: false })
    try {
      const pair = await authApi.refreshPair(refresh)
      setAccessToken(pair.access_token)
      setRefreshToken(pair.refresh_token)
      set({ user: await authApi.me(), initializing: false })
    } catch {
      clearTokens()
      set({ user: null, initializing: false })
    }
  },
}))

// A 401 that survives one refresh ends the session, wherever it happened.
setUnauthenticatedHandler(() => {
  clearTokens()
  useAuth.setState({ user: null })
})
