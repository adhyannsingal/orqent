import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { ThemeToggle } from '@/components/ThemeToggle'

/**
 * The centred card the unauthenticated pages share.
 *
 * Extracted when the reset flow added two more screens: three copies of the
 * same wordmark, spacing, and theme toggle would have drifted the moment one
 * of them was adjusted.
 */
export function AuthShell({
  title,
  subtitle,
  children,
}: {
  title: string
  subtitle: ReactNode
  children: ReactNode
}) {
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

        <h1 className="text-[19px] font-semibold tracking-tight">{title}</h1>
        {subtitle && <p className="mt-1 mb-5 text-[12.5px] text-ink-muted">{subtitle}</p>}
        {!subtitle && <div className="mb-5" />}

        {children}

        <p className="mt-3 text-center text-[12px] text-ink-muted">
          <Link to="/" className="underline-offset-2 hover:text-ink hover:underline">
            Back to landing
          </Link>
        </p>
      </div>
    </div>
  )
}
