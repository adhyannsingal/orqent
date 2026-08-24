import { useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { CheckCircle2 } from 'lucide-react'
import { authApi } from '@/api/auth'
import { messageOf } from '@/api/client'
import { Button, Field, Input } from '@/components/ui/primitives'
import { AuthShell } from './AuthShell'
import { PasswordChecklist, passwordPolicy, POLICY_MESSAGE } from './passwordPolicy'

/**
 * Choose a new password using a reset link.
 *
 * The token is read from the query string and held in component state for the
 * duration of the submit — never written to `localStorage`, `sessionStorage`,
 * a cookie, or a log. It is a bearer credential that changes a password, so
 * the shortest possible residence is the correct one.
 *
 * On success the URL is replaced rather than pushed, which drops the
 * token-bearing entry from history: the link is spent, and leaving it in the
 * back stack invites it to be re-opened, re-submitted, and shoulder-surfed for
 * no benefit.
 *
 * No session is established. The backend deliberately returns no tokens — it
 * has just revoked every one the user had — so the flow ends at sign-in.
 */
export function ResetPasswordPage() {
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const token = params.get('token') ?? ''

  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [done, setDone] = useState(false)
  const policy = passwordPolicy(password)

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    setError(null)
    if (!policy.valid) {
      setError(POLICY_MESSAGE)
      return
    }
    if (password !== confirm) {
      setError('The two passwords do not match.')
      return
    }
    setBusy(true)
    try {
      await authApi.resetPassword(token, password)
      setDone(true)
      // Drop the spent token from the address bar and from history.
      navigate('/reset-password', { replace: true })
    } catch (caught) {
      // The backend gives one message for every invalid-link case; showing it
      // verbatim is correct precisely because it distinguishes nothing.
      setError(messageOf(caught, 'Could not reset your password. Please try again.'))
    } finally {
      setBusy(false)
    }
  }

  if (done) {
    return (
      <AuthShell title="Password updated" subtitle={null}>
        <div className="flex items-start gap-2.5 rounded-sm border border-line bg-surface px-3 py-2.5">
          <CheckCircle2 className="mt-px size-4 shrink-0 text-status-succeeded" />
          <p className="text-[12.5px] leading-relaxed text-ink-muted">
            Your password has been changed and every other session was signed out. Sign in with
            your new password to continue.
          </p>
        </div>
        <p className="mt-4 text-center text-[12px] text-ink-muted">
          <Link to="/login" className="font-medium text-ink underline-offset-2 hover:underline">
            Go to sign in
          </Link>
        </p>
      </AuthShell>
    )
  }

  if (!token) {
    return (
      <AuthShell title="Reset link missing" subtitle={null}>
        <p className="text-[12.5px] leading-relaxed text-ink-muted">
          This page needs the link from your reset email. Request a new one if the link has
          expired.
        </p>
        <p className="mt-4 text-center text-[12px] text-ink-muted">
          <Link
            to="/forgot-password"
            className="font-medium text-ink underline-offset-2 hover:underline"
          >
            Request a reset link
          </Link>
        </p>
      </AuthShell>
    )
  }

  return (
    <AuthShell title="Choose a new password" subtitle="This signs out every other session.">
      <form onSubmit={submit} className="space-y-3">
        <Field label="New password">
          <Input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="••••••••"
            required
            autoComplete="new-password"
          />
        </Field>
        <Field label="Confirm new password">
          <Input
            type="password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            placeholder="••••••••"
            required
            autoComplete="new-password"
          />
        </Field>
        <PasswordChecklist policy={policy} />

        {error && (
          <div className="rounded-sm border border-orange-200 bg-orange-50 px-2.5 py-2 text-[12px] text-status-failed">
            {error}
          </div>
        )}

        <Button type="submit" variant="primary" loading={busy} className="w-full">
          Set new password
        </Button>
      </form>

      <p className="mt-4 text-center text-[12px] text-ink-muted">
        <Link to="/login" className="font-medium text-ink underline-offset-2 hover:underline">
          Back to sign in
        </Link>
      </p>
    </AuthShell>
  )
}
