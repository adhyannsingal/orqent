import { useState } from 'react'
import { Link } from 'react-router-dom'
import { MailCheck } from 'lucide-react'
import { authApi } from '@/api/auth'
import { Button, Field, Input } from '@/components/ui/primitives'
import { ThemeToggle } from '@/components/ThemeToggle'
import { AuthShell } from './AuthShell'

/**
 * Request a password-reset link.
 *
 * The one rule this page exists to honour: the success state is reached
 * whatever the backend found. There is no "no account with that email" branch,
 * because rendering one would hand back exactly the answer the endpoint
 * refuses to give — a login form that reveals nothing is pointless next to a
 * reset form that reveals everything.
 *
 * That also means a network or server failure is the *only* thing that can
 * show an error here.
 */
export function ForgotPasswordPage() {
  const [email, setEmail] = useState('')
  const [sent, setSent] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    setError(null)
    setBusy(true)
    try {
      await authApi.forgotPassword(email)
      setSent(true)
    } catch {
      // Deliberately not `messageOf`: the only failures that reach here are
      // transport-level, and echoing a backend body would risk surfacing
      // something account-specific if the API ever changed.
      setError('Could not reach the server. Please try again.')
    } finally {
      setBusy(false)
    }
  }

  if (sent) {
    return (
      <AuthShell title="Check your email" subtitle={null}>
        <div className="flex items-start gap-2.5 rounded-sm border border-line bg-surface px-3 py-2.5">
          <MailCheck className="mt-px size-4 shrink-0 text-status-succeeded" />
          <p className="text-[12.5px] leading-relaxed text-ink-muted">
            If an account exists for <span className="text-ink">{email}</span>, a password reset
            link has been sent. The link expires in 30 minutes.
          </p>
        </div>
        <p className="mt-4 text-center text-[12px] text-ink-muted">
          <Link to="/login" className="font-medium text-ink underline-offset-2 hover:underline">
            Back to sign in
          </Link>
        </p>
      </AuthShell>
    )
  }

  return (
    <AuthShell
      title="Reset your password"
      subtitle="We'll email you a link to choose a new one."
    >
      <form onSubmit={submit} className="space-y-3">
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

        {error && (
          <div className="rounded-sm border border-orange-200 bg-orange-50 px-2.5 py-2 text-[12px] text-status-failed">
            {error}
          </div>
        )}

        <Button type="submit" variant="primary" loading={busy} className="w-full">
          Send reset link
        </Button>
      </form>

      <p className="mt-4 text-center text-[12px] text-ink-muted">
        Remembered it?{' '}
        <Link to="/login" className="font-medium text-ink underline-offset-2 hover:underline">
          Sign in
        </Link>
      </p>
    </AuthShell>
  )
}

export { ThemeToggle }
