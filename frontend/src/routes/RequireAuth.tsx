import { Navigate, useLocation } from 'react-router-dom'
import { useAuth } from '@/stores/auth'
import { Spinner } from '@/components/ui/primitives'

/**
 * Gate for the authenticated shell.
 *
 * Waits for the session-restore attempt to settle before deciding, so a reload
 * does not bounce an authenticated user to the login screen. The redirect
 * carries no `next` parameter — an unvalidated redirect target is a well-known
 * open-redirect footgun, and the app has one sensible destination anyway.
 */
export function RequireAuth({ children }: { children: React.ReactNode }) {
  const user = useAuth((s) => s.user)
  const initializing = useAuth((s) => s.initializing)
  const location = useLocation()

  if (initializing) {
    return (
      <div className="grid h-full place-items-center">
        <Spinner label="Restoring session" />
      </div>
    )
  }
  if (!user) return <Navigate to="/login" replace state={{ from: location.pathname }} />
  return <>{children}</>
}
