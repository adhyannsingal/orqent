/**
 * The small set of primitives the whole app is built from.
 *
 * Hand-written rather than pulled from a component library: the design brief
 * asks for a compact, technical look with small radii and thin borders, and
 * every generic library default (large radius, soft shadow, roomy padding)
 * would have to be overridden anyway.
 */
import { cn } from '@/lib/utils'
import { Loader2 } from 'lucide-react'
import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode, TextareaHTMLAttributes } from 'react'

type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger'
type ButtonSize = 'sm' | 'md'

const BUTTON_VARIANTS: Record<ButtonVariant, string> = {
  primary: 'border border-[#2563eb] bg-[#2563eb] text-white hover:border-[#1d4ed8] hover:bg-[#1d4ed8] disabled:border-ink-muted disabled:bg-ink-muted',
  secondary:
    'bg-surface text-ink border border-line-strong hover:bg-canvas',
  ghost: 'text-ink-muted hover:text-ink hover:bg-canvas',
  danger: 'bg-status-failed text-white hover:brightness-95',
}

const BUTTON_SIZES: Record<ButtonSize, string> = {
  sm: 'h-7 px-2.5 text-[12.5px] gap-1.5',
  md: 'h-8 px-3 text-[13px] gap-2',
}

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant
  size?: ButtonSize
  loading?: boolean
}

export function Button({
  variant = 'secondary', size = 'md', loading, className, children, disabled, ...rest
}: ButtonProps) {
  return (
    <button
      {...rest}
      disabled={disabled || loading}
      className={cn(
        'inline-flex items-center justify-center rounded-sm font-medium',
        'transition-colors focus-visible:outline-2 focus-visible:outline-offset-1',
        'focus-visible:outline-accent disabled:opacity-60 disabled:cursor-not-allowed',
        BUTTON_VARIANTS[variant], BUTTON_SIZES[size], className,
      )}
    >
      {loading && <Loader2 className="size-3.5 animate-spin" />}
      {children}
    </button>
  )
}

export function Input({ className, ...rest }: InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      {...rest}
      className={cn(
        'h-8 w-full rounded-sm border border-line-strong bg-surface',
        'px-2.5 text-[13px] text-ink placeholder:text-ink-muted',
        'focus:border-accent focus:outline-none focus:ring-2 focus:ring-accent/15',
        'disabled:bg-canvas disabled:text-ink-muted', className,
      )}
    />
  )
}

export function Textarea({ className, ...rest }: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return (
    <textarea
      {...rest}
      className={cn(
        'w-full rounded-sm border border-line-strong bg-surface',
        'px-2.5 py-2 text-[13px] leading-relaxed text-ink',
        'placeholder:text-ink-muted focus:border-accent focus:outline-none',
        'focus:ring-2 focus:ring-accent/15', className,
      )}
    />
  )
}

export function Label({ children, hint }: { children: ReactNode; hint?: string }) {
  return (
    <label className="mb-1 flex items-baseline justify-between gap-2">
      <span className="text-[12px] font-medium text-ink">{children}</span>
      {hint && <span className="text-[11px] text-ink-muted">{hint}</span>}
    </label>
  )
}

export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <div>
      <Label hint={hint}>{label}</Label>
      {children}
    </div>
  )
}

export function Card({ className, children }: { className?: string; children: ReactNode }) {
  return (
    <div className={cn('rounded-md border border-line bg-surface', className)}>
      {children}
    </div>
  )
}

export function EmptyState({
  icon, title, description, action,
}: { icon?: ReactNode; title: string; description?: string; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center px-6 py-16 text-center">
      {icon && <div className="mb-3 text-ink-muted">{icon}</div>}
      <p className="text-[14px] font-medium text-ink">{title}</p>
      {description && (
        <p className="mt-1 max-w-sm text-[12.5px] text-ink-muted">{description}</p>
      )}
      {action && <div className="mt-4">{action}</div>}
    </div>
  )
}

export function Spinner({ label }: { label?: string }) {
  return (
    <div className="flex items-center justify-center gap-2 py-12 text-ink-muted">
      <Loader2 className="size-4 animate-spin" />
      {label && <span className="text-[12.5px]">{label}</span>}
    </div>
  )
}

/** A dialog. Deliberately plain: node editing uses the inspector, so dialogs
 *  are reserved for genuinely modal moments (create, publish result). */
export function Modal({
  open, onClose, title, description, children, width = 'max-w-md',
}: {
  open: boolean; onClose: () => void; title: string
  description?: string; children: ReactNode; width?: string
}) {
  if (!open) return null
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/30 p-4 pt-[12vh]">
      <div
        className="absolute inset-0"
        onClick={onClose}
        aria-hidden
      />
      <div
        role="dialog"
        aria-modal="true"
        className={cn(
          'relative w-full rounded-md border border-line',
          'bg-surface shadow-lg', width,
        )}
      >
        <div className="border-b border-line px-4 py-3">
          <h2 className="text-[13.5px] font-semibold text-ink">{title}</h2>
          {description && (
            <p className="mt-0.5 text-[12px] text-ink-muted">{description}</p>
          )}
        </div>
        <div className="p-4">{children}</div>
      </div>
    </div>
  )
}
