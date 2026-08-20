import { useState } from 'react'
import { Link, Navigate, useLocation, useNavigate } from 'react-router-dom'
import { Check, Circle } from 'lucide-react'
import { useAuth } from '@/stores/auth'
import { Button, Field, Input } from '@/components/ui/primitives'
import { messageOf } from '@/api/client'
import { ThemeToggle } from '@/components/ThemeToggle'

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
  const location = useLocation()

  const mode = location.pathname === '/register' ? 'register' : 'login'
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [organization, setOrganization] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const policy = passwordPolicy(password)

  if (user) return <Navigate to="/workflows" replace />

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    setError(null)
    if (mode === 'register' && !policy.valid) {
      setError('Password must include 8+ characters, a letter, a number, and a special character.')
      return
    }
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
      <div className="absolute right-5 top-5">
        <ThemeToggle />
      </div>
      <div className="w-full max-w-[340px]">
        <div className="mb-7 flex items-center gap-2">
          <div className="grid size-6 place-items-center rounded-[4px] bg-ink">
            <span className="text-[13px] font-bold leading-none text-white">Ø</span>
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
          {mode === 'register' && <PasswordChecklist policy={policy} />}

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
          <Link
            to={mode === 'login' ? '/register' : '/login'}
            onClick={() => setError(null)}
            className="font-medium text-ink underline-offset-2 hover:underline"
          >
            {mode === 'login' ? 'Create one' : 'Sign in'}
          </Link>
        </p>
        <p className="mt-3 text-center text-[12px] text-ink-muted">
          <Link to="/" className="underline-offset-2 hover:text-ink hover:underline">
            Back to landing
          </Link>
        </p>
      </div>
    </div>
  )
}

function passwordPolicy(password: string) {
  const length = password.length >= 8
  const letter = /\p{L}/u.test(password)
  const number = /\p{N}/u.test(password)
  const special = /[^\p{L}\p{N}\s]/u.test(password)
  return { length, letter, number, special, valid: length && letter && number && special }
}

function PasswordChecklist({ policy }: { policy: ReturnType<typeof passwordPolicy> }) {
  return (
    <div className="rounded-sm border border-line bg-surface px-2.5 py-2">
      <p className="mb-1 text-[11.5px] font-medium text-ink-muted">Password must include:</p>
      <div className="grid gap-1 text-[11.5px]">
        <PasswordRule met={policy.length} label="8+ characters" />
        <PasswordRule met={policy.letter} label="a letter" />
        <PasswordRule met={policy.number} label="a number" />
        <PasswordRule met={policy.special} label="a special character" />
      </div>
    </div>
  )
}

function PasswordRule({ met, label }: { met: boolean; label: string }) {
  return (
    <span className={met ? 'flex items-center gap-1.5 text-status-succeeded' : 'flex items-center gap-1.5 text-ink-muted'}>
      {met ? <Check className="size-3" /> : <Circle className="size-3" />}
      {label}
    </span>
  )
}
