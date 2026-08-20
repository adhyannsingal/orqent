import { useState } from 'react'
import { Navigate, useNavigate } from 'react-router-dom'
import { useAuth } from '@/stores/auth'
import { Button, Field, Input } from '@/components/ui/primitives'
import { messageOf } from '@/api/client'

/**
 * Sign in and sign up, against the real auth routes.
 *
 * Registration creates an organization as well as a user — that is the
 * backend's `RegisterRequest`, and it is why the form asks for a workspace
 * name. Nothing here stores a credential itself; the store and the API client
 * own that.
 */
export function LoginPage() {
  const user = useAuth((s) => s.user)
  const login = useAuth((s) => s.login)
  const register = useAuth((s) => s.register)
  const navigate = useNavigate()

  const [mode, setMode] = useState<'login' | 'register'>('login')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [organization, setOrganization] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  if (user) return <Navigate to="/workflows" replace />

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    setError(null)
    setBusy(true)
    try {
      if (mode === 'login') await login(email, password)
      else await register(email, password, organization)
      navigate('/workflows', { replace: true })
    } catch (caught) {
      // A user-safe message from the error envelope — never a raw body.
      setError(messageOf(caught, 'Could not sign in. Check your details and try again.'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex h-full items-center justify-center bg-canvas px-4">
      <div className="w-full max-w-[340px]">
        <div className="mb-7 flex items-center gap-2">
          <div className="grid size-6 place-items-center rounded-[4px] bg-ink">
            <span className="text-[12px] font-bold leading-none text-white">O</span>
          </div>
          <span className="text-[16px] font-semibold tracking-tight">Orqent</span>
        </div>

        <h1 className="text-[19px] font-semibold tracking-tight">
          {mode === 'login' ? 'Sign in' : 'Create your workspace'}
        </h1>
        <p className="mt-1 mb-5 text-[12.5px] text-ink-muted">
          {mode === 'login'
            ? 'Build, run, and inspect workflow automations.'
            : 'Your workspace is the tenant every workflow and document belongs to.'}
        </p>

        <form onSubmit={submit} className="space-y-3">
          {mode === 'register' && (
            <Field label="Workspace name">
              <Input
                value={organization}
                onChange={(e) => setOrganization(e.target.value)}
                placeholder="Acme Inc"
                required
                autoComplete="organization"
              />
            </Field>
          )}
          <Field label="Email">
            <Input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@company.com"
              required
              autoComplete="email"
            />
          </Field>
          <Field label="Password">
            <Input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="••••••••"
              required
              autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
            />
          </Field>

          {error && (
            <div className="rounded-sm border border-orange-200 bg-orange-50 px-2.5 py-2 text-[12px] text-status-failed">
              {error}
            </div>
          )}

          <Button type="submit" variant="primary" loading={busy} className="w-full">
            {mode === 'login' ? 'Sign in' : 'Create workspace'}
          </Button>
        </form>

        <p className="mt-4 text-center text-[12px] text-ink-muted">
          {mode === 'login' ? "Don't have a workspace?" : 'Already have one?'}{' '}
          <button
            onClick={() => { setMode(mode === 'login' ? 'register' : 'login'); setError(null) }}
            className="font-medium text-ink underline-offset-2 hover:underline"
          >
            {mode === 'login' ? 'Create one' : 'Sign in'}
          </button>
        </p>
      </div>
    </div>
  )
}
