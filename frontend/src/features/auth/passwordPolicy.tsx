import { Check, Circle } from 'lucide-react'

/**
 * The password rule, mirrored from the backend for UX only.
 *
 * One definition on this side too: sign-up and password reset both import it,
 * so the checklist a user sees while choosing a password cannot disagree with
 * the one they see while resetting it. The backend remains authoritative —
 * `enforce_password_complexity` in `app/schemas/auth.py` is what actually
 * decides, and this exists so the user finds out before submitting rather than
 * after.
 *
 * `\p{L}` and `\p{N}` match Python's `isalpha()`/`isnumeric()` closely enough
 * for a hint; where they diverge the server's answer wins, which is why the
 * form still renders whatever the API says on rejection.
 */
export function passwordPolicy(password: string) {
  const length = password.length >= 8
  const letter = /\p{L}/u.test(password)
  const number = /\p{N}/u.test(password)
  const special = /[^\p{L}\p{N}\s]/u.test(password)
  return { length, letter, number, special, valid: length && letter && number && special }
}

export const POLICY_MESSAGE =
  'Password must include 8+ characters, a letter, a number, and a special character.'

export function PasswordChecklist({ policy }: { policy: ReturnType<typeof passwordPolicy> }) {
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
    <span
      className={
        met
          ? 'flex items-center gap-1.5 text-status-succeeded'
          : 'flex items-center gap-1.5 text-ink-muted'
      }
    >
      {met ? <Check className="size-3" /> : <Circle className="size-3" />}
      {label}
    </span>
  )
}
