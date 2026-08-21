import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter } from 'react-router-dom'
import { Toaster } from 'sonner'
import '@xyflow/react/dist/style.css'
import './index.css'
import { App } from './App'
import { ThemeProvider, useTheme } from '@/components/ThemeProvider'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // A workflow list does not change while you look at it; a run does, and
      // asks for polling explicitly.
      staleTime: 10_000,
      refetchOnWindowFocus: false,
      retry: (failureCount, error) => {
        // Never retry an auth or client error into a loop.
        const status = (error as { status?: number })?.status
        if (status && status < 500) return false
        return failureCount < 2
      },
    },
  },
})

function ThemedToaster() {
  const { resolved } = useTheme()
  return (
    <Toaster
      position="bottom-right"
      theme={resolved}
      toastOptions={{
        style: {
          borderRadius: '6px',
          border: '1px solid var(--color-line-strong)',
          background: 'var(--color-surface)',
          color: 'var(--color-ink)',
          fontSize: '12.5px',
        },
      }}
    />
  )
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ThemeProvider>
      <QueryClientProvider client={queryClient}>
        <BrowserRouter>
          <App />
          <ThemedToaster />
        </BrowserRouter>
      </QueryClientProvider>
    </ThemeProvider>
  </StrictMode>,
)
