import { Moon, Sun } from 'lucide-react'
import { useTheme } from '@/components/ThemeProvider'
import { cn } from '@/lib/utils'

export function ThemeToggle({ className }: { className?: string }) {
  const { resolved, toggle } = useTheme()
  const dark = resolved === 'dark'

  return (
    <button
      type="button"
      onClick={toggle}
      aria-label={dark ? 'Switch to light theme' : 'Switch to dark theme'}
      title={dark ? 'Switch to light theme' : 'Switch to dark theme'}
      className={cn(
        'inline-flex h-8 items-center gap-2 rounded-full border border-line-strong',
        'bg-surface px-2.5 text-[12px] font-medium text-ink-muted transition-colors',
        'hover:bg-canvas hover:text-ink focus-visible:outline-2 focus-visible:outline-offset-1',
        'focus-visible:outline-accent',
        className,
      )}
    >
      {dark ? <Moon className="size-3.5" /> : <Sun className="size-3.5" />}
      <span>{dark ? 'Dark' : 'Light'}</span>
    </button>
  )
}
