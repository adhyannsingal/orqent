import {
  createContext, useContext, useEffect, useLayoutEffect, useMemo, useState,
  type ReactNode,
} from 'react'

type ThemePreference = 'system' | 'light' | 'dark'
type ResolvedTheme = 'light' | 'dark'

interface ThemeContextValue {
  preference: ThemePreference
  resolved: ResolvedTheme
  setPreference: (preference: ThemePreference) => void
  toggle: () => void
}

const STORAGE_KEY = 'orqent.theme'
const ThemeContext = createContext<ThemeContextValue | null>(null)

function readPreference(): ThemePreference {
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    return stored === 'light' || stored === 'dark' || stored === 'system' ? stored : 'system'
  } catch {
    return 'system'
  }
}

function systemTheme(): ResolvedTheme {
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

function applyTheme(theme: ResolvedTheme): void {
  document.documentElement.classList.toggle('dark', theme === 'dark')
  document.documentElement.style.colorScheme = theme
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [preference, setPreferenceState] = useState<ThemePreference>(() => readPreference())
  const [system, setSystem] = useState<ResolvedTheme>(() => systemTheme())
  const resolved = preference === 'system' ? system : preference

  useLayoutEffect(() => {
    applyTheme(resolved)
  }, [resolved])

  useEffect(() => {
    const query = window.matchMedia?.('(prefers-color-scheme: dark)')
    if (!query) return undefined
    const update = () => setSystem(query.matches ? 'dark' : 'light')
    update()
    query.addEventListener('change', update)
    return () => query.removeEventListener('change', update)
  }, [])

  const value = useMemo<ThemeContextValue>(() => ({
    preference,
    resolved,
    setPreference: (next) => {
      setPreferenceState(next)
      try {
        localStorage.setItem(STORAGE_KEY, next)
      } catch {
        /* theme persistence is a preference, not a requirement */
      }
    },
    toggle: () => {
      const next = resolved === 'dark' ? 'light' : 'dark'
      setPreferenceState(next)
      try {
        localStorage.setItem(STORAGE_KEY, next)
      } catch {
        /* storage unavailable; the visual toggle still works for this session */
      }
    },
  }), [preference, resolved])

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}

export function useTheme() {
  const context = useContext(ThemeContext)
  if (!context) throw new Error('useTheme must be used within ThemeProvider')
  return context
}
