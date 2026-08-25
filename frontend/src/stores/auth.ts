import { create } from 'zustand'
import { authApi } from '@/api/auth'
import {
  clearAccessToken,
  refreshSession,
  setAccessToken,
  setUnauthenticatedHandler,
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
    // The refresh token is not in this response: the backend set it as an
    // HttpOnly cookie, and the browser is already holding it.
    const issued = await authApi.login(email, password)
    setAccessToken(issued.access_token)
    set({ user: await authApi.me() })
  },

  register: async (email, password, organizationName) => {
    // Registration does not authenticate on its own — it returns a user, not a
    // session — so it is followed by a real login, which is what establishes
    // the cookie. Unchanged by AH3 beyond where the refresh token now lives.
    await authApi.register(email, password, organizationName)
    const issued = await authApi.login(email, password)
    setAccessToken(issued.access_token)
    set({ user: await authApi.me() })
  },

  logout: async () => {
    // Now a genuine server call rather than a courtesy: only the backend can
    // revoke the family and delete the HttpOnly cookie. Still best effort, so
    // an offline user is not trapped in a session they asked to leave — but if
    // it fails, the cookie survives and a reload would restore the session,
    // which is the honest consequence of the server owning session state.
    await authApi.logout().catch(() => undefined)
    clearAccessToken()
    set({ user: null })
  },

  /**
   * Re-establish a session on load, from the HttpOnly refresh cookie.
   *
   * The access token is deliberately not persisted, so this exchange is what
   * makes a reload survivable at all. There is nothing to check first: the
   * cookie is invisible to this code, so the only way to ask "am I still
   * signed in?" is to try.
   *
   * **Goes through `refreshSession`, not a bare API call.** That routes it
   * through the same in-flight deduplication the 401 path uses, and it is
   * load-bearing rather than tidy: React's StrictMode invokes this effect
   * twice in development, and two independent refreshes would carry the *same*
   * cookie. The first rotates it; the second is then a replay, which the
   * backend correctly treats as a stolen token and answers by revoking the
   * whole family — logging the user out on every reload. Sharing one request
   * makes the second caller await the first's result instead.
   */
  restore: async () => {
    if (await refreshSession()) {
      set({ user: await authApi.me(), initializing: false })
      return
    }
    clearAccessToken()
    set({ user: null, initializing: false })
  },
}))

// A 401 that survives one refresh ends the session, wherever it happened.
setUnauthenticatedHandler(() => {
  clearAccessToken()
  useAuth.setState({ user: null })
})
